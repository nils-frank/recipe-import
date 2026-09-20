"""Bildstufe: aus einem fertigen Rezept ein Bild des Gerichts erzeugen.

Feature A19, `openspec/changes/add-ai-recipe-image`. Drei der vier Importwege legen ein
Rezept ganz ohne Bild an - der JSON-LD-Weg (`schema.to_jsonld` kennt kein Bildfeld), der
Uploadweg und YouTube. In Mealies Kachelansicht bleibt davon eine graue Fläche, und Name
und Bild sind das Einzige, was vor dem Öffnen eines Rezepts zu sehen ist.

Aufbau bewusst wie die Namensstufe (`naming.py`, A18), eine Schicht weiter aussen:
ein Schalter, ein Aufruf, jeder Fehlschlag endet in `None` und einer Warnung. Diese
Stufe darf einen Import **nie** scheitern lassen - sie läuft erst, wenn das Rezept in
Mealie schon existiert und `store.finish()` gelaufen ist.

Anders als `llm._post` ohne Wiederholung: dort kostet ein Fehlschlag den ganzen Import,
hier nur ein Bild. Und ein Bildaufruf ist die mit Abstand teuerste Anfrage dieses
Dienstes, ein zweiter Anlauf also nichts, was nebenbei passieren darf.

Zwei Anbieterformen, gewählt über `config.IMAGE_PROVIDER`, weil sie sich nicht
ineinander übersetzen lassen:

* `pollinations` (Vorgabe): ein `GET {IMAGE_BASE_URL}/prompt/{Prompt}`, der Prompt steht
  urlkodiert im Pfad, der Antwortkörper **ist** das Bild. Kein JSON in beide Richtungen,
  kein Schlüssel nötig.
* `openai`: `POST {IMAGE_BASE_URL}/images/generations`, JSON hin und zurück, Bild als
  `data[0].b64_json` oder ersatzweise unter `data[0].url`.

Probe vom 2026-09-20 (Aufgabe 1.3 der Änderung):

* Der eingetragene Gemini-Schlüssel kann **keine** Bilder erzeugen - Einzelheiten in
  `config.IMAGE_PROVIDER`. Deshalb ist Pollinations die Vorgabe, nicht der Anbieter des
  Textmodells.
* Pollinations ohne Konto und ohne Schlüssel: 200 `image/jpeg`, 768x768, 46-73 KB,
  35-46 s je Bild (vier Messungen). Nur das Modell `sana`; `flux` und `turbo` werden
  angenommen, aber vom selben Modell beantwortet.
* Ohne Token wirken `nologo`, `width` und `height` nicht: jedes Bild ist 768x768 und
  trägt unten rechts ein `pollinations.ai`-Wasserzeichen. Mit einem kostenlosen Token in
  `IMAGE_API_KEY` (zweite Probe desselben Tages) ändert sich nur die Grösse, das
  Wasserzeichen bleibt - dafür braucht es eine bezahlte Stufe des Dienstes. `nologo=true`
  geht trotzdem mit, damit eine höhere Stufe sofort wirkt; `width` und `height` schickt
  diese Stufe nicht, 768x768 passt in Mealies Kachelansicht.
* Zwei gleichzeitige Anfragen beantwortet der Dienst mit 429. Ein Import erzeugt
  höchstens ein Bild, das trifft also nur überlappende Importe - dann bleibt das Rezept
  eben ohne Bild.
"""
from __future__ import annotations

import base64
import logging
from urllib.parse import quote

import requests

import config
import naming
import prompts
from llm import _image_mime

log = logging.getLogger(__name__)

# Bildmodelle rechnen länger als Textmodelle, aber die Stufe hält die Push-Meldung auf
# (app._publish meldet erst, wenn das Rezept fertig ist). Die Messung oben liegt bei
# 35-46 s; 90 s lassen Luft, ohne dass jemand ewig auf die Meldung wartet.
TIMEOUT_SECONDS = 90

# Zeit für den Nachschlag, wenn ein OpenAI-kompatibler Anbieter statt der Bilddaten eine
# URL liefert. Kurz, weil das reines Herunterladen ist.
DOWNLOAD_TIMEOUT_SECONDS = 30

# Der Prompt ist ein Satz und steht bei Pollinations im URL-Pfad. Ein langer Prompt macht
# das Bild nicht besser, sondern schlechter (siehe prompts.py), deshalb gehen nur die
# ersten Zutaten mit und jede gekürzt.
MAX_INGREDIENTS = 10
MAX_INGREDIENT_CHARS = 60

# Grösser als das ist keine Antwort mehr, sondern ein Versehen - und der Container läuft
# mit mem_limit 512m (DESIGN.md §10).
MAX_IMAGE_BYTES = 12 * 1024 * 1024


def _shorten_body(resp: requests.Response) -> str:
    """Antwortkörper für die Protokollzeile: gekürzt und ohne Kopfzeilen, damit kein
    Schlüssel in ein Protokoll gerät (wie in `llm._post`)."""
    try:
        return resp.text[:300]
    except Exception:  # noqa: BLE001 - Protokollzeile darf nie werfen  # pragma: no cover
        return "(nicht lesbar)"


def _prompt(name: str, ingredients: list[str]) -> str:
    return prompts.IMAGE_GENERATION_PROMPT_TEMPLATE.format(
        name=" ".join((name or "").split()) or "(ohne Namen)",
        # Dieselbe Kürzung wie in der Namensstufe statt einer zweiten Umsetzung davon,
        # nur in einer Zeile: der Prompt ist ein Satz.
        ingredients=", ".join(
            naming._shorten(ingredients, MAX_INGREDIENTS, MAX_INGREDIENT_CHARS).splitlines()
        ),
    )


def _fetch_pollinations(prompt: str) -> bytes:
    """Bilddaten von Pollinations.ai. Wirft bei jeder Antwort, die kein Bild ist."""
    base = config.IMAGE_BASE_URL.rstrip("/")
    # safe="" - auch "/" muss kodiert werden, sonst zerfällt der Prompt in Pfadteile.
    url = f"{base}/prompt/{quote(prompt, safe='')}"
    headers = {}
    if config.IMAGE_API_KEY:
        headers["Authorization"] = f"Bearer {config.IMAGE_API_KEY}"

    resp = requests.get(
        url,
        params={"model": config.IMAGE_MODEL, "nologo": "true"},
        headers=headers,
        timeout=TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}: {_shorten_body(resp)}")

    content_type = resp.headers.get("Content-Type", "")
    if not content_type.startswith("image/"):
        raise ValueError(f"Antwort war {content_type or 'ohne Typ'}, kein Bild: {_shorten_body(resp)}")
    return resp.content


def _fetch_openai_compatible(prompt: str) -> bytes:
    """Bilddaten von einem OpenAI-kompatiblen Anbieter."""
    base = config.IMAGE_BASE_URL.rstrip("/")
    resp = requests.post(
        f"{base}/images/generations",
        headers={
            "Authorization": f"Bearer {config.IMAGE_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": config.IMAGE_MODEL,
            "prompt": prompt,
            "n": 1,
            "response_format": "b64_json",
        },
        timeout=TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}: {_shorten_body(resp)}")
    return _decode(resp.json())


def _decode(payload: dict) -> bytes:
    """Bilddaten aus der JSON-Antwort eines OpenAI-kompatiblen Anbieters. Wirft bei jeder
    unerwarteten Form."""
    items = payload.get("data")
    if not isinstance(items, list) or not items:
        raise ValueError("Antwort enthielt kein Feld 'data' mit Einträgen")
    first = items[0]
    if not isinstance(first, dict):
        raise ValueError("Der erste Eintrag in 'data' war kein Objekt")

    encoded = first.get("b64_json")
    if isinstance(encoded, str) and encoded.strip():
        return base64.b64decode(encoded, validate=True)

    url = first.get("url")
    if isinstance(url, str) and url.strip():
        # Manche Anbieter liefern nur einen Verweis. Ein GET mehr ist billiger als eine
        # verworfene Bilderzeugung, die bereits bezahlt ist.
        log.info("Anbieter lieferte eine URL statt Bilddaten, lade sie nach")
        resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            raise ValueError(
                f"Das Bild war unter der gelieferten URL nicht abrufbar: HTTP {resp.status_code}"
            )
        return resp.content

    raise ValueError("Der Eintrag in 'data' trug weder 'b64_json' noch 'url'")


def generate(name: str, ingredients: list[str]) -> bytes | None:
    """Erzeugt ein Bild des Gerichts. `None`, wenn das aus irgendeinem Grund nicht
    gelingt oder die Stufe abgeschaltet ist.

    Wirft nie.
    """
    if not config.IMAGE_ENABLED:
        log.info("Bildstufe ist per IMAGE_ENABLED abgeschaltet, kein Bild für %r", name)
        return None

    prompt = _prompt(name, ingredients)
    fetch = _fetch_pollinations if config.IMAGE_PROVIDER == "pollinations" else _fetch_openai_compatible

    log.info("Bilderzeugung für %r bei %s, Modell %s", name, config.IMAGE_PROVIDER, config.IMAGE_MODEL)
    try:
        data = fetch(prompt)
    except requests.RequestException as exc:
        log.warning("Bildmodell nicht erreichbar, Rezept %r bleibt ohne Bild: %s", name, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - Bildstufe darf den Import nie scheitern lassen
        log.warning("Antwort des Bildmodells unbrauchbar, Rezept %r bleibt ohne Bild: %s", name, exc)
        return None

    if not data:
        log.warning("Das Bildmodell lieferte leere Bilddaten, Rezept %r bleibt ohne Bild", name)
        return None
    if len(data) > MAX_IMAGE_BYTES:
        log.warning(
            "Das gelieferte Bild ist mit %d Bytes zu gross (Grenze %d), Rezept %r bleibt ohne Bild",
            len(data), MAX_IMAGE_BYTES, name,
        )
        return None

    try:
        mime = _image_mime(data)
    except Exception as exc:  # noqa: BLE001 - Bildstufe darf den Import nie scheitern lassen
        log.warning("Das Bildmodell lieferte kein lesbares Bild, Rezept %r bleibt ohne Bild: %s", name, exc)
        return None

    log.info("Bild für %r erzeugt: %d Bytes, %s", name, len(data), mime)
    return data
