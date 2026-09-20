"""LLM stage: raw text in, validated Recipe out.

OpenAI-compatible chat completion against LLM_BASE_URL. The schema is enforced
by the provider via response_format=json_schema, fed from
Recipe.model_json_schema() - there is exactly one schema definition (SPEC 4),
the prompt in prompts.py never restates it.

Retry policy is deliberately flat: one validation failure gets exactly one
second attempt with the Pydantic error appended, then LlmError. No loop. A
model that misses the schema twice will not find it on the fifth try, and every
attempt costs money on a path whose only authentication is a webhook ID
(SPEC 11).

Separately, _post retries once on a transient HTTP status (429/500/502/503/504)
before either of the above ever runs - that is the provider itself saying
"try later", not a malformed answer.

If that second attempt fails too, _post walks the model chain (A21,
model_chain.py): the same unchanged call goes to the next model, because on
this provider "out of quota" and "under load" are per-model conditions, not
account-wide ones. Only when the whole chain is used up does LlmOverloadedError
come out, so app.py still gives the SPEC 7 wording for exactly the case it
meant. The classification per status is the table further down.

extract_recipe_from_images (A12) is the same call with images instead of text:
same endpoint, same enforced schema, same retry rule, same prompt core plus one
rule about reading only what is actually on the picture.
"""
from __future__ import annotations

import base64
import copy
import json
import logging
import time
from typing import Any

import requests
from pydantic import ValidationError

import config
import model_chain
import prompts
from schema import Recipe

log = logging.getLogger(__name__)

# HTTP-Stufen, bei denen der Anbieter selbst sagt "spaeter nochmal" statt "das war
# falsch": 429 (Rate-Limit), 502/503/504 (Overload/Gateway), 500 (oft ebenfalls
# voruebergehend bei den grossen Anbietern). Ein einziger Wiederholungsversuch auf
# demselben Modell, siehe _post - das ist ein anderer Fall als die Schema-Wiederholung
# unten (_Invalid): dort war die Antwort da und falsch geformt, hier kam gar keine
# Antwort durch.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_TRANSIENT_RETRY_DELAY_SECONDS = 2

# Was der zweite vergebliche Versuch bedeutet, haengt an der Stufe (A21, design.md):
#
#   429            Das Kontingent *dieses* Modells ist leer. Modell sperren und
#                  denselben Aufruf an das naechste Modell der Kette schicken.
#   502/503/504    *Dieses* Modell ist gerade unter Last. Gemessen am 2026-09-20:
#                  gemini-3.7-flash antwortete zweimal 503 "high demand", waehrend
#                  gemini-3.8-flash und gemini-3.5-flash im selben Moment 200
#                  antworteten. Also weiter zum naechsten Modell - aber **ohne**
#                  Sperrfrist: Last vergeht in Minuten, ein leeres Kontingent nicht.
#   500            Nennt keine modellabhaengige Ursache. Verhalten wie bisher, kein
#                  Modellwechsel.
_EXHAUSTING_STATUS = {429}
_MODEL_BUSY_STATUS = {502, 503, 504}

# Stufen, mit denen der Anbieter sagt, dass er diesen Modellnamen nicht (mehr) kennt.
# 404 ist der gemessene Fall: gemini-2.5-flash antwortet "no longer available to new
# users". Ein 400 kann dasselbe meinen, sagt es aber nur im Text - deshalb die Marker.
_UNKNOWN_MODEL_STATUS = {404}
_UNKNOWN_MODEL_MARKERS = (
    "no longer available",
    "is not found",
    "not found for api version",
    "unknown model",
    "does not exist",
    "is not supported",
)


def _means_unknown_model(status: int, body: str) -> bool:
    """Heisst diese Antwort "den Modellnamen gibt es hier nicht"?

    Der Text wird nur bei 400 befragt. Bei 404 ist die Stufe schon eindeutig, und bei
    allen uebrigen Stufen waere die Textsuche Raten - ein 429 mit dem Wort "not found"
    im Hilfelink bliebe sonst als unbekanntes Modell haengen.
    """
    if status in _UNKNOWN_MODEL_STATUS:
        return True
    if status != 400:
        return False
    text = body.lower()
    return any(marker in text for marker in _UNKNOWN_MODEL_MARKERS)


# Grosszuegig, weil eine Untertitelspur mit LLM-Aufruf im Hintergrund laeuft und
# niemand darauf wartet. Der HTTP-Aufruf ist die einzige langsame Stelle im Ablauf.
TIMEOUT_SECONDS = 120

# Bilder brauchen laenger: das Modell muss erst lesen, was auf der Seite steht (SPEC 6).
IMAGE_TIMEOUT_SECONDS = 180

# Ein Untertitel einer langen Kochsendung kann das Kontextfenster sprengen. Der
# Rezeptteil steht praktisch immer am Anfang, deshalb wird hinten abgeschnitten
# statt den Aufruf scheitern zu lassen.
MAX_TEXT_CHARS = 60_000


class LlmError(RuntimeError):
    """Die Rezepterkennung ist gescheitert. Der Text ist fuer Menschen gedacht."""


class LlmOverloadedError(LlmError):
    """Der Anbieter hat auch nach dem einen Wiederholungsversuch noch mit einer
    voruebergehenden Stufe (429/500/502/503/504) geantwortet - siehe
    _RETRYABLE_STATUS. Eigene Klasse analog zu ThrottledError (youtube.py, A15):
    kein Formatfehler, sondern "spaeter nochmal", app.py gibt dafuer den Wortlaut
    aus DESIGN.md §7 statt der generischen LlmError-Meldung aus."""


class NoRecipeFoundError(LlmError):
    """Das Modell hat in der Quelle kein Rezept gefunden. Kein Formatfehler und kein
    Fehlschlag des Dienstes, deshalb eine eigene Klasse: app.py gibt dafuer den
    passenden Wortlaut aus SPEC 7 aus statt "Die Rezepterkennung ist gescheitert"."""


class _Invalid(Exception):
    """Antwort passt nicht zum Schema. Loest genau einen zweiten Versuch aus."""


def _strict_schema() -> dict[str, Any]:
    """Recipe.model_json_schema() in der Form, die json_schema/strict verlangt.

    Rein mechanische Umformung, keine zweite Schema-Definition: additionalProperties
    ausschliessen und alle Eigenschaften als required fuehren. Optionale Felder sind
    im Pydantic-Schema bereits als anyOf mit null modelliert und bleiben damit
    weglassbar, indem das Modell null einsetzt.
    """
    schema = copy.deepcopy(Recipe.model_json_schema())
    schema["additionalProperties"] = False
    schema["required"] = list(schema.get("properties", {}).keys())
    return schema


def _post(
    messages: list[dict[str, Any]],
    timeout: int = TIMEOUT_SECONDS,
    schema: dict[str, Any] | None = None,
    schema_name: str = "Recipe",
    model: str | None = None,
) -> str:
    """Der gemeinsame Transportweg zum Modell. Rohtext der Antwort, kein Parsen.

    `schema` und `schema_name` sind Argumente, damit die Namensstufe (naming.py,
    Feature A18) denselben Weg nutzt statt einen zweiten HTTP-Aufruf mit eigener
    Fehlerbehandlung aufzumachen. Ohne Angabe gilt das Recipe-Schema aus DESIGN.md §4.

    Zwei geschachtelte Schleifen (A21): aussen die Modellkette aus `model_chain`,
    innen die beiden Versuche auf demselben Modell, die es hier immer schon gab. Was
    der zweite vergebliche Versuch bedeutet, steht in der Tabelle oben am Modul.
    Derselbe Aufruf geht unveraendert an das naechste Modell - gleicher Prompt,
    gleiches erzwungenes Schema, gleiche Pruefung.

    `model` haengt die Kette aus und spricht genau einen Namen an. Nur fuer die Probe
    der Modellsuche (`model_chain.refresh`): ein Modell, das noch gar nicht in der
    Kette steht, soll weder ihre Reihenfolge noch ihre Sperrfristen anfassen.
    """
    base = config.LLM_BASE_URL.rstrip("/")
    payload = {
        "model": None,  # je Kandidat gesetzt, siehe unten
        "messages": messages,
        # Rezeptextraktion ist Wiedergabe, keine Textproduktion. Jede Kreativitaet
        # hier waere eine erfundene Menge.
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": _strict_schema() if schema is None else schema,
            },
        },
    }

    pinned = model is not None
    if pinned:
        kandidaten = [model]
    else:
        kandidaten = model_chain.candidates()
        if not kandidaten:
            raise _nothing_left_error()

    # Fuer die Abschlussmeldung, wenn die Kette durch ist: was wurde probiert, und
    # was hat das letzte Modell gesagt.
    versucht: list[str] = []
    letzter_status: int | None = None
    letzter_transienter_text: str | None = None
    nur_unbekannt = True

    for kandidat in kandidaten:
        if versucht:
            log.warning(
                "Modellwechsel: %s -> %s nach HTTP %s",
                versucht[-1], kandidat, letzter_status,
            )
        versucht.append(kandidat)
        payload["model"] = kandidat
        response = None

        for attempt in (1, 2):
            try:
                response = requests.post(
                    f"{base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {config.LLM_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                # Kein Modellproblem, sondern gar keine Verbindung - das naechste
                # Modell liegt hinter derselben Leitung.
                raise LlmError(f"Das Sprachmodell war nicht erreichbar: {exc}") from exc

            status = response.status_code
            if status == 200:
                return _content(response)

            # Antwortkoerper gekuerzt und ohne Kopfzeilen, damit kein Schluessel in
            # eine Protokollzeile geraet.
            koerper = response.text[:300]
            detail = f"Das Sprachmodell antwortete mit HTTP {status}: {koerper}"

            if _means_unknown_model(status, koerper):
                # Kein zweiter Versuch: ein Name, den es nicht gibt, entsteht nicht
                # durch Wiederholen.
                letzter_status = status
                if not pinned:
                    model_chain.mark_unknown(kandidat)
                    break
                raise LlmError(detail)

            nur_unbekannt = False

            if status in _RETRYABLE_STATUS:
                letzter_status = status
                letzter_transienter_text = detail
                if attempt == 1:
                    log.warning(
                        "Modell %s antwortete mit HTTP %d (voruebergehend), "
                        "ein zweiter Versuch nach %ds",
                        kandidat, status, _TRANSIENT_RETRY_DELAY_SECONDS,
                    )
                    time.sleep(_TRANSIENT_RETRY_DELAY_SECONDS)
                    continue
                if pinned:
                    raise LlmOverloadedError(detail)
                if status in _EXHAUSTING_STATUS:
                    model_chain.mark_exhausted(kandidat)
                    break
                if status in _MODEL_BUSY_STATUS:
                    # Last dieses Modells, keine Sperrfrist - siehe Tabelle oben.
                    break
                # 500: nennt keine modellabhaengige Ursache, also wie bisher Schluss.
                raise LlmOverloadedError(detail)

            # Jede andere Stufe ist ein echter Fehler dieses Aufrufs (falsches Schema,
            # fehlender Schluessel, zu grosse Anfrage) und auf dem naechsten Modell
            # genauso falsch.
            raise LlmError(detail)

    if letzter_transienter_text is not None:
        raise LlmOverloadedError(
            f"{letzter_transienter_text} (versucht: {', '.join(versucht)})"
        )
    if nur_unbekannt:
        raise LlmError(
            "Der Anbieter kennt keines der eingestellten Sprachmodelle: "
            + ", ".join(versucht)
        )
    raise _nothing_left_error()


def _nothing_left_error() -> LlmError:
    """Die Kette ist leer, bevor ueberhaupt ein Aufruf hinausging.

    Zwei Ursachen mit zwei verschiedenen Meldungen: sind alle Namen unbekannt, ist das
    ein Konfigurationsfehler und liest sich als Fehlschlag; sind sie gesperrt, ist es
    "spaeter nochmal" und geht als LlmOverloadedError durch denselben Weg wie ein
    ausgelasteter Anbieter (DESIGN.md §7).
    """
    kette = model_chain.configured()
    if kette and set(kette) <= model_chain.unknown():
        return LlmError(
            "Der Anbieter kennt keines der eingestellten Sprachmodelle: " + ", ".join(kette)
        )
    return LlmOverloadedError(
        "Alle eingestellten Sprachmodelle sind derzeit erschoepft: " + ", ".join(kette)
    )


def _content(response: requests.Response) -> str:
    """Die eine brauchbare Zeichenkette aus einer 200-Antwort."""
    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        raise LlmError(f"Die Antwort des Sprachmodells war unlesbar: {exc}") from exc

    if not content or not content.strip():
        raise LlmError("Das Sprachmodell hat eine leere Antwort geliefert.")
    return content


def _parse(
    content: str, source_url: str, empty_message: str = "Im Text war kein Rezept zu finden."
) -> Recipe:
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise _Invalid(f"Antwort war kein gueltiges JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _Invalid("Antwort war kein JSON-Objekt.")

    # Der Prompt erlaubt ausdruecklich eine leere Antwort, wenn im Text kein Rezept
    # steht. Das ist eine Aussage des Modells, kein Formatfehler - ein zweiter
    # Versuch wuerde es nur zum Erfinden draengen.
    if (
        not str(data.get("name") or "").strip()
        and not data.get("recipeIngredient")
        and not data.get("recipeInstructions")
    ):
        raise NoRecipeFoundError(empty_message)

    # Die Quell-URL kommt vom Aufrufer, nicht vom Modell.
    data["url"] = source_url
    try:
        return Recipe.model_validate(data)
    except ValidationError as exc:
        raise _Invalid(str(exc)) from exc


def extract_recipe(text: str, source_url: str) -> Recipe:
    """Liest ein Rezept aus Rohtext. Wirft LlmError, wenn das nicht gelingt."""
    if not text or not text.strip():
        raise LlmError("Der Quelltext war leer.")

    if len(text) > MAX_TEXT_CHARS:
        log.warning(
            "Quelltext von %s auf %d Zeichen gekuerzt (war %d)", source_url, MAX_TEXT_CHARS, len(text)
        )
        text = text[:MAX_TEXT_CHARS]

    messages = [
        {"role": "system", "content": prompts.SYSTEM_PROMPT},
        {"role": "user", "content": prompts.USER_PROMPT_TEMPLATE.format(source_url=source_url, text=text)},
    ]

    log.info(
        "LLM-Extraktion fuer %s, %d Zeichen, Modell %s",
        source_url, len(text), model_chain.head(),
    )
    content = _post(messages)
    try:
        return _parse(content, source_url)
    except _Invalid as exc:
        log.warning("LLM-Antwort fuer %s ungueltig, ein zweiter Versuch: %s", source_url, exc)
        first_error = str(exc)

    messages.append({"role": "assistant", "content": content})
    messages.append({"role": "user", "content": prompts.RETRY_PROMPT_TEMPLATE.format(error=first_error)})

    content = _post(messages)
    try:
        return _parse(content, source_url)
    except _Invalid as exc:
        raise LlmError(
            f"Die Antwort des Sprachmodells passte auch im zweiten Versuch nicht zum Schema: {exc}"
        ) from exc


# Magic Bytes statt des vom Client behaupteten Inhaltstyps: die data:-URI muss den
# tatsaechlichen Typ nennen, sonst weist das Modell das Bild ab. document.py hat den
# Typ bereits gegen die erlaubte Liste geprueft, hier geht es nur um die Kodierung.
def _image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise LlmError("Das Bildformat war nicht lesbar (weder JPEG, PNG noch WebP).")


def extract_recipe_from_images(images: list[bytes], source: str) -> Recipe:
    """Liest ein Rezept von einem oder mehreren Bildern.

    Derselbe Endpunkt, dasselbe erzwungene Schema und dieselbe Retry-Regel wie
    extract_recipe (SPEC 6). Unterschied ist allein der Inhalt der Nutzernachricht: statt
    Text eine Liste von image_url-Teilen mit data:-URIs. Der zweite Versuch schickt die
    Bilder nicht erneut - sie stehen bereits im Nachrichtenverlauf, ein zweites Mal waere
    doppelte Bildkosten fuer dieselbe Information.
    """
    if not images:
        raise LlmError("Es war kein Bild dabei.")

    parts: list[dict[str, Any]] = [
        {"type": "text", "text": prompts.IMAGE_USER_PROMPT_TEMPLATE.format(source=source)}
    ]
    for data in images:
        encoded = base64.b64encode(data).decode("ascii")
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{_image_mime(data)};base64,{encoded}"},
            }
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": prompts.SYSTEM_PROMPT + "\n\n" + prompts.IMAGE_RULE},
        {"role": "user", "content": parts},
    ]

    total_bytes = sum(len(d) for d in images)
    log.info(
        "LLM-Bilderkennung fuer %s, %d Bild(er), %d Bytes, Modell %s",
        source, len(images), total_bytes, model_chain.head(),
    )

    empty_message = "Auf dem Bild war kein Rezept zu erkennen."
    content = _post(messages, timeout=IMAGE_TIMEOUT_SECONDS)
    try:
        return _parse(content, source, empty_message)
    except _Invalid as exc:
        log.warning("LLM-Bildantwort fuer %s ungueltig, ein zweiter Versuch: %s", source, exc)
        first_error = str(exc)

    messages.append({"role": "assistant", "content": content})
    messages.append({"role": "user", "content": prompts.RETRY_PROMPT_TEMPLATE.format(error=first_error)})

    content = _post(messages, timeout=IMAGE_TIMEOUT_SECONDS)
    try:
        return _parse(content, source, empty_message)
    except _Invalid as exc:
        raise LlmError(
            f"Die Antwort des Sprachmodells passte auch im zweiten Versuch nicht zum Schema: {exc}"
        ) from exc
