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
"try later", not a malformed answer. Raises LlmOverloadedError if the second
attempt also comes back transient, so app.py can give the SPEC 7 wording
instead of the generic LlmError text.

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
import prompts
from schema import Recipe

log = logging.getLogger(__name__)

# HTTP-Stufen, bei denen der Anbieter selbst sagt "spaeter nochmal" statt "das war
# falsch": 429 (Rate-Limit), 502/503/504 (Overload/Gateway), 500 (oft ebenfalls
# voruebergehend bei den grossen Anbietern). Ein einziger Wiederholungsversuch, siehe
# _post - das ist ein anderer Fall als die Schema-Wiederholung unten (_Invalid): dort
# war die Antwort da und falsch geformt, hier kam gar keine Antwort durch.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_TRANSIENT_RETRY_DELAY_SECONDS = 2

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
) -> str:
    """Der gemeinsame Transportweg zum Modell. Rohtext der Antwort, kein Parsen.

    `schema` und `schema_name` sind Argumente, damit die Namensstufe (naming.py,
    Feature A18) denselben Weg nutzt statt einen zweiten HTTP-Aufruf mit eigener
    Fehlerbehandlung aufzumachen. Ohne Angabe gilt das Recipe-Schema aus DESIGN.md §4.
    """
    base = config.LLM_BASE_URL.rstrip("/")
    payload = {
        "model": config.LLM_MODEL,
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
            raise LlmError(f"Das Sprachmodell war nicht erreichbar: {exc}") from exc

        if response.status_code == 200:
            break
        if response.status_code in _RETRYABLE_STATUS and attempt == 1:
            log.warning(
                "LLM antwortete mit HTTP %d (voruebergehend), ein zweiter Versuch nach %ds",
                response.status_code, _TRANSIENT_RETRY_DELAY_SECONDS,
            )
            time.sleep(_TRANSIENT_RETRY_DELAY_SECONDS)
            continue
        # Antwortkoerper gekuerzt und ohne Kopfzeilen, damit kein Schluessel in
        # eine Protokollzeile geraet.
        detail = f"Das Sprachmodell antwortete mit HTTP {response.status_code}: {response.text[:300]}"
        if response.status_code in _RETRYABLE_STATUS:
            raise LlmOverloadedError(detail)
        raise LlmError(detail)

    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        raise LlmError(f"Die Antwort des Sprachmodells war unlesbar: {exc}") from exc

    if not content or not content.strip():
        raise LlmError("Das Sprachmodell hat eine leere Antwort geliefert.")
    return content


def _parse(content: str, source_url: str, empty_message: str = "Im Text war kein Rezept zu finden.") -> Recipe:
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise _Invalid(f"Antwort war kein gueltiges JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _Invalid("Antwort war kein JSON-Objekt.")

    # Der Prompt erlaubt ausdruecklich eine leere Antwort, wenn im Text kein Rezept
    # steht. Das ist eine Aussage des Modells, kein Formatfehler - ein zweiter
    # Versuch wuerde es nur zum Erfinden draengen.
    if not str(data.get("name") or "").strip() and not data.get("recipeIngredient") and not data.get("recipeInstructions"):
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
        log.warning("Quelltext von %s auf %d Zeichen gekuerzt (war %d)", source_url, MAX_TEXT_CHARS, len(text))
        text = text[:MAX_TEXT_CHARS]

    messages = [
        {"role": "system", "content": prompts.SYSTEM_PROMPT},
        {"role": "user", "content": prompts.USER_PROMPT_TEMPLATE.format(source_url=source_url, text=text)},
    ]

    log.info("LLM-Extraktion fuer %s, %d Zeichen, Modell %s", source_url, len(text), config.LLM_MODEL)
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
        raise LlmError(f"Die Antwort des Sprachmodells passte auch im zweiten Versuch nicht zum Schema: {exc}") from exc


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
        source, len(images), total_bytes, config.LLM_MODEL,
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
        raise LlmError(f"Die Antwort des Sprachmodells passte auch im zweiten Versuch nicht zum Schema: {exc}") from exc
