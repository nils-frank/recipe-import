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
import time
from urllib.parse import quote

import requests

import config
import image_chain
import naming
import prompts
from llm import _UNKNOWN_MODEL_MARKERS, _image_mime

# Einzige Zeitquelle der Stufe, damit die Testreihe das Zeitbudget ohne Schlafen prüfen
# kann (DESIGN.md §12). `monotonic` wie in `image_chain`.
_now = time.monotonic

log = logging.getLogger(__name__)

# Bildmodelle rechnen länger als Textmodelle, aber die Stufe hält die Push-Meldung auf
# (app._publish meldet erst, wenn das Rezept fertig ist). Die Messung oben liegt bei
# 35-46 s; 90 s lassen Luft, ohne dass jemand ewig auf die Meldung wartet.
TIMEOUT_SECONDS = 90

# Gemini antwortet deutlich schneller: 3,2 s bis 15,9 s je Bild in der Probe vom
# 2026-09-20 (A22, Aufgabe 1.2). 60 s sind das Vierfache des gemessenen schlechtesten
# Falls - wer länger braucht, hat den Import verloren, nicht das Bild.
GEMINI_TIMEOUT_SECONDS = 60

_TIMEOUTS = {
    "gemini": GEMINI_TIMEOUT_SECONDS,
    "openai": TIMEOUT_SECONDS,
    "pollinations": TIMEOUT_SECONDS,
}

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


class ProviderError(RuntimeError):
    """Der Anbieter hat mit einer Fehlerstufe geantwortet.

    Trägt Stufe und (gekürzten) Körper, weil erst der Aufrufer entscheidet, was daraus
    folgt: Guthaben leer, Modell unbekannt, oder einfach unbrauchbar. Ein Fehlschlag ohne
    Antwort (Zeitüberschreitung, kein Netz) ist `requests.RequestException` und kommt hier
    gar nicht an.
    """

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


def _endpoint(kandidat: tuple[str, str]) -> tuple[str, str]:
    """Adresse und Schlüssel des Anbieters dieses Kandidaten (`config.IMAGE_ENDPOINTS`).

    Je Anbieter, nicht global: eine Kette kann in einem Import mehrere Anbieter ansprechen,
    und der Schlüssel des einen darf dabei nie beim anderen landen.
    """
    return config.IMAGE_ENDPOINTS[kandidat[0]]


def _fetch_pollinations(prompt: str, kandidat: tuple[str, str], timeout: float) -> bytes:
    """Bilddaten von Pollinations.ai. Wirft bei jeder Antwort, die kein Bild ist."""
    basis, schluessel = _endpoint(kandidat)
    base = basis.rstrip("/")
    # safe="" - auch "/" muss kodiert werden, sonst zerfällt der Prompt in Pfadteile.
    url = f"{base}/prompt/{quote(prompt, safe='')}"
    headers = {}
    if schluessel:
        headers["Authorization"] = f"Bearer {schluessel}"

    resp = requests.get(
        url,
        params={"model": kandidat[1], "nologo": "true"},
        headers=headers,
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, _shorten_body(resp))

    content_type = resp.headers.get("Content-Type", "")
    if not content_type.startswith("image/"):
        raise ValueError(f"Antwort war {content_type or 'ohne Typ'}, kein Bild: {_shorten_body(resp)}")
    return resp.content


def _fetch_gemini(prompt: str, kandidat: tuple[str, str], timeout: float) -> bytes:
    """Bilddaten von Gemini über die **native** Fläche.

    Nicht über `/images/generations`: dort bildet dieser Anbieter auf `predict` ab, was
    keines seiner Modelle führt (404, gemessen 2026-09-20). Gemessene Form der Anfrage und
    der Antwort, kein Lesen der Dokumentation.

    `aspectRatio: "1:1"` liefert 1024x1024 statt der Vorgabe 1408x768, **zum selben Preis**
    (beides 1120 Bildtoken). Mealies Kachel würde das breitere Bild ohnehin beschneiden.
    """
    basis, schluessel = _endpoint(kandidat)
    base = basis.rstrip("/")
    resp = requests.post(
        f"{base}/v1beta/models/{kandidat[1]}:generateContent",
        headers={"x-goog-api-key": schluessel, "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {"aspectRatio": "1:1"},
            },
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, _shorten_body(resp))
    return _decode_gemini(resp.json())


def _decode_gemini(payload: dict) -> bytes:
    """Bilddaten aus der Antwort der nativen Fläche. Wirft bei jeder unerwarteten Form.

    Die Antwort kann Text **und** Bild tragen, und sie kann ganz ohne Bildteil kommen -
    ein Modell, das lieber antwortet, statt zu malen. Das ist keine Ausnahme des Anbieters,
    sondern eine unbrauchbare Antwort: der Aufrufer geht zum nächsten Kandidaten.
    """
    kandidaten = payload.get("candidates")
    if not isinstance(kandidaten, list) or not kandidaten:
        raise ValueError("Antwort enthielt kein Feld 'candidates' mit Einträgen")

    teile = []
    for eintrag in kandidaten:
        if isinstance(eintrag, dict):
            inhalt = eintrag.get("content")
            if isinstance(inhalt, dict) and isinstance(inhalt.get("parts"), list):
                teile.extend(inhalt["parts"])

    for teil in teile:
        if not isinstance(teil, dict):
            continue
        inline = teil.get("inlineData") or teil.get("inline_data")
        if not isinstance(inline, dict):
            continue
        kodiert = inline.get("data")
        if isinstance(kodiert, str) and kodiert.strip():
            return base64.b64decode(kodiert, validate=True)

    raise ValueError("Antwort trug keinen Teil mit Bilddaten")


def _fetch_openai_compatible(prompt: str, kandidat: tuple[str, str], timeout: float) -> bytes:
    """Bilddaten von einem OpenAI-kompatiblen Anbieter."""
    basis, schluessel = _endpoint(kandidat)
    base = basis.rstrip("/")
    resp = requests.post(
        f"{base}/images/generations",
        headers={
            "Authorization": f"Bearer {schluessel}",
            "Content-Type": "application/json",
        },
        json={
            "model": kandidat[1],
            "prompt": prompt,
            "n": 1,
            "response_format": "b64_json",
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise ProviderError(resp.status_code, _shorten_body(resp))
    return _decode(resp.json())


_FETCHERS = {
    "gemini": _fetch_gemini,
    "openai": _fetch_openai_compatible,
    "pollinations": _fetch_pollinations,
}


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


# Stufen, mit denen ein Anbieter sagt "dein Kontingent, dein Guthaben oder deine Rate ist
# aufgebraucht". 429 ist der gemessene Fall bei beiden Anbietern; 402 und 403 nennen den
# Grund nur im Text, deshalb dort die Marker.
_EXHAUSTING_STATUS = {429}
_EXHAUSTING_MARKERS = ("quota", "credit", "billing", "resource_exhausted", "exceeded")


def _classify(exc: ProviderError) -> str:
    """Was bedeutet diese Fehlerantwort für den Kandidaten?

    Drei Ausgänge, weil sie drei verschiedene Gedächtnisdauern haben: "erschöpft" gilt für
    die Sperrfrist, "unbekannt" für die Lebensdauer des Prozesses, "unbrauchbar" nur für
    diesen einen Import. Die Marker für den unbekannten Modellnamen sind dieselben wie in
    der Textstufe (`llm._UNKNOWN_MODEL_MARKERS`) - ein zurückgezogener Name soll in beiden
    Stufen gleich erkannt werden.
    """
    text = exc.body.lower()
    if exc.status in _EXHAUSTING_STATUS:
        return "erschöpft"
    if exc.status in (402, 403) and any(marker in text for marker in _EXHAUSTING_MARKERS):
        return "erschöpft"
    if exc.status == 404 or (exc.status == 400 and any(m in text for m in _UNKNOWN_MODEL_MARKERS)):
        return "unbekannt"
    return "unbrauchbar"


def _usable(data: bytes, name: str, kandidat: tuple[str, str]) -> str | None:
    """Prüft die Bilddaten und gibt den erkannten MIME-Typ zurück, oder `None`.

    Dieselben drei Prüfungen wie bisher, nur jetzt je Kandidat: leer, zu gross, oder keine
    Magic Bytes eines Bildes. Alle drei heissen "unbrauchbar", nicht "Fehler".
    """
    if not data:
        log.warning("%s:%s lieferte leere Bilddaten für %r", kandidat[0], kandidat[1], name)
        return None
    if len(data) > MAX_IMAGE_BYTES:
        log.warning(
            "%s:%s lieferte %d Bytes für %r, mehr als die Grenze %d",
            kandidat[0], kandidat[1], len(data), name, MAX_IMAGE_BYTES,
        )
        return None
    try:
        return _image_mime(data)
    except Exception as exc:  # noqa: BLE001 - Bildstufe darf den Import nie scheitern lassen
        log.warning(
            "%s:%s lieferte kein lesbares Bild für %r: %s", kandidat[0], kandidat[1], name, exc
        )
        return None


def generate(name: str, ingredients: list[str]) -> bytes | None:
    """Erzeugt ein Bild des Gerichts. `None`, wenn das aus irgendeinem Grund nicht
    gelingt oder die Stufe abgeschaltet ist.

    Geht die Kette von oben nach unten durch und hört beim ersten brauchbaren Bild auf.
    Höchstens **ein** Aufruf je Kandidat: ein Bildaufruf kostet Geld, und der nächste
    Kandidat ist der bessere zweite Versuch. Die Liste wird einmal am Anfang geholt, also
    kann derselbe Kandidat innerhalb eines Imports gar nicht zweimal drankommen.

    Wirft nie.
    """
    if not config.IMAGE_ENABLED:
        log.info("Bildstufe ist per IMAGE_ENABLED abgeschaltet, kein Bild für %r", name)
        return None

    kandidaten = image_chain.candidates()
    if not kandidaten:
        # Zwei verschiedene Lagen, zwei verschiedene Meldungen: alles gesperrt heisst
        # "später nochmal", eine leere Kette heisst "falsch konfiguriert".
        if image_chain.configured():
            log.warning("Alle Bildkandidaten sind gerade gesperrt, %r bleibt ohne Bild", name)
        else:
            log.warning("Die Bildkette ist leer, %r bleibt ohne Bild", name)
        return None

    prompt = _prompt(name, ingredients)
    frist = _now() + config.IMAGE_DEADLINE_SECONDS

    for kandidat in kandidaten:
        anbieter, modell = kandidat
        timeout = _TIMEOUTS[anbieter]
        rest = frist - _now()
        if rest < timeout:
            # Bewusst kein gekürztes Zeitlimit: ein bezahlter Aufruf, der nicht zu Ende
            # laufen darf, ist Geld für nichts. Lieber ohne Bild fertig werden.
            log.warning(
                "Zeitbudget der Bildstufe aufgebraucht (%.0fs übrig, %s:%s braucht %ds), "
                "%r bleibt ohne Bild",
                max(rest, 0.0), anbieter, modell, timeout, name,
            )
            return None

        log.info("Bilderzeugung für %r bei %s, Modell %s", name, anbieter, modell)
        try:
            data = _FETCHERS[anbieter](prompt, kandidat, timeout)
        except ProviderError as exc:
            grund = _classify(exc)
            if grund == "erschöpft":
                image_chain.mark_exhausted(kandidat)
            elif grund == "unbekannt":
                image_chain.mark_unknown(kandidat)
            log.warning("%s:%s antwortete %s (%s), weiter zum nächsten Kandidaten",
                        anbieter, modell, exc.status, grund)
            continue
        except requests.RequestException as exc:
            log.warning("%s:%s war nicht erreichbar (%s), weiter zum nächsten Kandidaten",
                        anbieter, modell, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - Bildstufe darf den Import nie scheitern lassen
            log.warning("%s:%s antwortete unbrauchbar (%s), weiter zum nächsten Kandidaten",
                        anbieter, modell, exc)
            continue

        mime = _usable(data, name, kandidat)
        if mime is None:
            continue

        log.info("Bild für %r von %s:%s erzeugt: %d Bytes, %s",
                 name, anbieter, modell, len(data), mime)
        return data

    log.warning("Kein Kandidat der Bildkette lieferte ein Bild, %r bleibt ohne Bild", name)
    return None
