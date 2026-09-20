"""Konfiguration ausschliesslich über Umgebungsvariablen. Kein Modul liest
Umgebungsvariablen ausserhalb dieser Datei. `os.environ.get` mit Vorgabewert, plus
`require()` für Pflichtwerte, die beim Start hart abbricht statt später mit einem
unklaren Fehler zu scheitern.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path


def require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Pflicht-Umgebungsvariable {name} fehlt")
    return value


MEALIE_URL = os.environ.get("MEALIE_URL", "http://localhost:30081")
MEALIE_TOKEN = require("MEALIE_TOKEN")

HA_URL = os.environ.get("HA_URL", "http://localhost:8123")
HA_TOKEN = require("HA_TOKEN")
# notify.iphone_2 / notify.send_message sind bewusst nicht die Vorgabe, siehe
# ha_notify.py: sie melden nur `supported_features: 1` und tragen kein `data`-Feld,
# koennen also keinen klickbaren Mealie-Link zustellen.
HA_NOTIFY_TARGET = require("HA_NOTIFY_TARGET")

# Wechselt später auf den LiteLLM-Proxy, siehe DESIGN.md §3.
LLM_BASE_URL = os.environ.get(
    "LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
)
# Das einzelne Modell von früher. Bleibt definiert und in seiner Bedeutung unverändert -
# es steht in Protokollzeilen und trägt eine bestehende .env unverändert weiter. Welches
# Modell ein Aufruf tatsächlich nimmt, entscheidet zur Laufzeit die Kette unten
# (model_chain.py); dieser Wert ist deren Kopf, solange LLM_MODEL_CHAIN nichts anderes
# sagt.
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3.6-flash")
LLM_API_KEY = require("LLM_API_KEY")


# ---------------------------------------------------------------------------
# Modellkette der Textstufe (A21, openspec/changes/add-llm-model-fallback). Alle Werte
# optional: eine bestehende .env läuft unverändert weiter und gewinnt unterhalb ihres
# LLM_MODEL nur Rückfallmodelle dazu.
# ---------------------------------------------------------------------------

# Geordnete Liste, neuestes zuerst. Der erste Eintrag ist das normal genutzte Modell, der
# Rest sind Rückfälle, die nur drankommen, wenn ein früherer Eintrag nicht antwortet.
#
# Die vier Namen sind am 2026-09-20 gegen den eingetragenen Schlüssel gemessen, nicht
# geraten: alle vier existieren, gemini-3.8-flash und gemini-3.5-flash antworten 200,
# gemini-3.7-flash war gerade unter Last (503), und **gemini-3.6-flash - das bis dahin
# fest verdrahtete Modell - antwortet 429 "You exceeded your current quota"**. Genau
# dieser Fall hat jeden Import scheitern lassen.
_LLM_MODEL_CHAIN_DEFAULT = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
]


def _split_chain(raw: str) -> list[str]:
    """Kommaliste zu Namen, Leerraum weg, Leereinträge weg, Reihenfolge erhalten.
    Doppelte Namen fallen raus: die Kette ist eine Reihenfolge, kein Zähler."""
    seen: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def _resolve_model_chain() -> list[str]:
    """LLM_MODEL_CHAIN gewinnt, wenn gesetzt - wer die Kette ausdrücklich hinschreibt,
    meint genau sie.

    Sonst gilt die Vorgabeliste. Ein **ausdrücklich gesetztes** LLM_MODEL wandert dabei
    an die Spitze: eine bestehende .env, die ein Modell angepinnt hat, behält es als
    bevorzugtes und gewinnt die übrigen nur als Rückfall darunter - das ist der einzige
    Zweck der Verschiebung. Ist LLM_MODEL gar nicht gesetzt, bleibt die Vorgabeliste in
    ihrer Reihenfolge; ihr Vorgabewert allein ist keine Ansage.
    """
    explicit = os.environ.get("LLM_MODEL_CHAIN", "")
    if explicit.strip():
        chain = _split_chain(explicit)
        if chain:
            return chain
        # Ein Wert, der nur aus Kommas und Leerraum besteht, ist ein Tippfehler und
        # keine Ansage "gar kein Modell" - ohne Kette könnte kein Import laufen.
        logging.getLogger(__name__).warning(
            "LLM_MODEL_CHAIN nennt kein Modell, es gilt die Vorgabekette"
        )
        return list(_LLM_MODEL_CHAIN_DEFAULT)

    chain = list(_LLM_MODEL_CHAIN_DEFAULT)
    pinned = os.environ.get("LLM_MODEL", "").strip()
    if pinned:
        if pinned in chain:
            chain.remove(pinned)
        chain.insert(0, pinned)
    return chain


LLM_MODEL_CHAIN = _resolve_model_chain()

# Wie lange ein als erschöpft erkanntes Modell übersprungen wird. Eine Stunde ist reine
# Konfiguration, kein nachgebautes Anbieterverhalten (design.md, Offene Fragen): ein
# aufgebrauchtes Tageskontingent kostet damit einen abgelehnten Aufruf je Stunde statt
# einen je Import. Der Speicher liegt nur im laufenden Prozess, ein Neustart fängt wieder
# am Kopf der Kette an.
LLM_MODEL_COOLDOWN_SECONDS = float(os.environ.get("LLM_MODEL_COOLDOWN_SECONDS", "3600"))

# Übernimmt der Dienst von selbst neuere Modelle des Anbieters? Vorgabe an. Das kehrt die
# frühere Regel aus DESIGN.md §3 um ("fest verdrahtet, kein wandernder Alias"), deshalb
# ist die Übernahme an eine bestandene Schemaprobe gebunden, wird protokolliert, einmal
# per Push gemeldet - und hier in einer Zeile zurückgenommen. Dieselbe Wahrheitsprüfung
# wie NAMING_ENABLED.
LLM_MODEL_AUTODISCOVER = os.environ.get(
    "LLM_MODEL_AUTODISCOVER", "true"
).strip().lower() not in ("0", "false", "no", "off")

# Welche Einträge der Modellliste des Anbieters überhaupt als Textmodell in Frage kommen.
# Die beiden Gruppen sind Haupt- und Nebenversion und werden als Zahlen verglichen, nicht
# als Text - sonst stünde 3.10 unter 3.9. Der Ausdruck ist bewusst eng: er lässt
# gemini-3-flash-preview (keine Nebenversion), gemini-3.5-flash-lite,
# gemini-flash-latest (der wandernde Alias) und alle Bild-, Live- und TTS-Varianten
# draussen, die am 2026-09-20 in derselben Liste standen.
LLM_MODEL_PATTERN = os.environ.get("LLM_MODEL_PATTERN", r"^gemini-(\d+)\.(\d+)-flash$")

# Abstand zwischen zwei Durchläufen der Modellsuche, in Sekunden. Einmal am Tag: neue
# Modelle erscheinen in Wochen, nicht in Minuten, und jeder Durchlauf kostet eine
# Listenabfrage plus höchstens eine Probe.
LLM_MODEL_REFRESH_SECONDS = float(os.environ.get("LLM_MODEL_REFRESH_SECONDS", "86400"))

DB_PATH = os.environ.get("DB_PATH", "/data/recipe-import.db")

RATE_LIMIT_PER_HOUR = int(os.environ.get("RATE_LIMIT_PER_HOUR", "20"))

# Uploadweg POST /import/file (A12, DESIGN.md §6/§11). Dieser Endpunkt wird vom Kurzbefehl
# direkt aufgerufen, nicht ueber Home Assistant - ein Webhook-Ausloeser reicht keine
# Binaerdatei weiter. Deshalb traegt er seine eigene Authentifizierung. Pflichtwert, damit
# der Dienst nicht versehentlich ohne Token startet und dann offen im LAN steht.
IMPORT_TOKEN = require("IMPORT_TOKEN")

# Obergrenzen je Anfrage, hart durchgesetzt bevor irgendetwas gelesen wird: der Container
# laeuft mit mem_limit 512m (DESIGN.md §10), ein unbegrenzter Upload waere ein
# Speicherfehler statt einer Fehlermeldung.
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "12"))
MAX_UPLOAD_FILES = int(os.environ.get("MAX_UPLOAD_FILES", "4"))

# Mehr Seiten werden nicht gelesen - ein Rezept steht auf ein bis zwei Seiten, und der
# Rest waere nur Kontextfenster und Kosten.
MAX_PDF_PAGES = int(os.environ.get("MAX_PDF_PAGES", "10"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")


# Namensstufe (Feature A18, recipe-naming-plan.md §2.3). Vorgabe an: der Name ist das
# einzige Feld, das in Mealies Uebersicht sichtbar ist. Abschaltbar, weil die Stufe je
# Import einen zusaetzlichen LLM-Aufruf kostet und der Weg ueber import_url (Chefkoch und
# alles, was Mealie selbst schaben kann) bisher ganz ohne LLM auskam.
NAMING_ENABLED = os.environ.get("NAMING_ENABLED", "true").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)


# Bildstufe (Feature A19, openspec/changes/add-ai-recipe-image). Vorgabe an: nach dem
# Namen ist das Bild das Zweite, was Mealies Übersicht zeigt, und drei der vier Wege
# legen ein Rezept ganz ohne Bild an. Abschaltbar, weil ein Bildaufruf deutlich teurer
# ist als alle Textaufrufe dieses Dienstes zusammen - `IMAGE_ENABLED=false` ist die
# Rücknahme in einer Zeile. Dieselbe Wahrheitsprüfung wie NAMING_ENABLED.
IMAGE_ENABLED = os.environ.get("IMAGE_ENABLED", "true").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

# Anbieter der Bildstufe. Drei Formen, weil sie sich nicht ineinander übersetzen lassen:
#   "gemini"       - POST /v1beta/models/{Modell}:generateContent, Bild als inlineData.
#   "pollinations" - ein GET, dessen Antwortkörper das Bild ist, ohne Schlüssel.
#   "openai"       - POST /images/generations mit JSON hin und zurück.
#
# Erste Probe vom 2026-09-20 (A19) gegen den damals guthabenlosen Gemini-Schlüssel:
# `/images/generations` bildet dieser Anbieter auf `predict` ab, was keines seiner 58
# Modelle führt (404), und die Bildmodelle über `generateContent` antworteten 429
# "generate_content_free_tier_... limit: 0", auch mit einem Schlüssel aus einem frisch
# angelegten Projekt desselben Kontos. Deshalb war Pollinations die Vorgabe: 200
# image/jpeg, 768x768, 46-73 KB, 35-46 s je Bild, ohne Konto und ohne Schlüssel - aber
# mit Wasserzeichen.
#
# Zweite Probe desselben Tages (A22, Aufgabe 1.2), nachdem $5 Guthaben auf dem Konto
# lagen: **alle fünf Bildmodelle antworten 200 mit einem echten Bild**, ohne Wasserzeichen.
# gemini-3-pro-image 15,3 s und $0,134 je Bild, gemini-3.1-flash-image 8,6 s und $0,067,
# gemini-3.1-flash-lite-image 3,2 s und $0,034. Die Kette unten nimmt deshalb Gemini zuerst
# und Pollinations als kostenlosen Boden darunter.
#
# Ein unbekannter Wert fällt auf die Vorgabe zurück, statt den Dienst am Start scheitern zu
# lassen - die Bildstufe darf keinen Import kosten, erst recht keinen Start.
_IMAGE_PROVIDERS = ("gemini", "openai", "pollinations")

IMAGE_PROVIDER = os.environ.get("IMAGE_PROVIDER", "pollinations").strip().lower() or "pollinations"
if IMAGE_PROVIDER not in _IMAGE_PROVIDERS:
    logging.getLogger(__name__).warning(
        "Unbekannter IMAGE_PROVIDER %r, es gilt 'pollinations'", IMAGE_PROVIDER
    )
    IMAGE_PROVIDER = "pollinations"

# Fest verdrahtet wie LLM_MODEL, kein wandernder Alias: ein Modellwechsel ist eine
# bewusste Änderung. Ohne Token listet Pollinations genau ein Modell, `sana`; `flux` und
# `turbo` werden zwar angenommen, aber vom selben Modell beantwortet (Probe 2026-09-20).
_IMAGE_MODEL_DEFAULTS = {
    "gemini": "gemini-3-pro-image",
    "openai": "imagen-4.0-generate-001",
    "pollinations": "sana",
}
IMAGE_MODEL = os.environ.get("IMAGE_MODEL") or _IMAGE_MODEL_DEFAULTS[IMAGE_PROVIDER]


def _gemini_native_base() -> str:
    """Die native Fläche desselben Anbieters, aus LLM_BASE_URL abgeleitet.

    Bildmodelle antworten dort und **nicht** auf der OpenAI-kompatiblen Fläche, die
    LLM_BASE_URL benennt (gemessen: /images/generations -> 404). Abgeleitet statt zweimal
    konfiguriert, damit die beiden Adressen nicht auseinanderlaufen können; wer einen
    Proxy ohne `/openai` einträgt, bekommt dessen Adresse unverändert und kann sie
    weiterhin über IMAGE_BASE_URL überschreiben.
    """
    return LLM_BASE_URL.rstrip("/").removesuffix("/openai")


# Vorgaben je Anbieter, nicht global. Bei "gemini" und "openai" sind es die Werte des
# Textmodells: derselbe Anbieter, dasselbe Konto, dasselbe Guthaben. Bei "pollinations"
# bleibt der Schlüssel **leer** - der Dienst braucht keinen, und den Schlüssel des
# Textmodells an eine andere Firma zu schicken wäre ein Leck. Wer dort ein Konto hat,
# trägt seinen Token in IMAGE_API_KEY ein: er hebt die feste Kantenlänge 768 auf, das
# Wasserzeichen bleibt. Bewusst kein `require()` - die Bildstufe darf keinen neuen harten
# Startabbruch einführen.
_IMAGE_ENDPOINT_DEFAULTS = {
    "gemini": (_gemini_native_base(), LLM_API_KEY),
    "openai": (LLM_BASE_URL, LLM_API_KEY),
    "pollinations": ("https://image.pollinations.ai", ""),
}

IMAGE_BASE_URL = os.environ.get("IMAGE_BASE_URL") or _IMAGE_ENDPOINT_DEFAULTS[IMAGE_PROVIDER][0]
IMAGE_API_KEY = (
    os.environ.get("IMAGE_API_KEY") or _IMAGE_ENDPOINT_DEFAULTS[IMAGE_PROVIDER][1]
)

# Adresse und Schlüssel **je Anbieter**, weil die Kette unten mehrere Anbieter in einem
# Import ansprechen kann. IMAGE_BASE_URL und IMAGE_API_KEY behalten ihre Bedeutung und
# gelten weiter genau für den Anbieter, den IMAGE_PROVIDER nennt - eine einzelne
# Überschreibung auf alle Anbieter anzuwenden wäre der Weg, auf dem der Schlüssel des
# Textmodells bei Pollinations landet.
IMAGE_ENDPOINTS = dict(_IMAGE_ENDPOINT_DEFAULTS)
IMAGE_ENDPOINTS[IMAGE_PROVIDER] = (IMAGE_BASE_URL, IMAGE_API_KEY)


# ---------------------------------------------------------------------------
# Kette der Bildstufe (A22, openspec/changes/add-image-model-fallback). Alle Werte
# optional: eine bestehende .env läuft unverändert weiter und gewinnt unterhalb ihres
# Anbieters nur Rückfälle dazu.
# ---------------------------------------------------------------------------

# Geordnete Liste "Anbieter:Modell", bestes zuerst. Der erste Eintrag ist das normal
# genutzte Bildmodell, der Rest sind Rückfälle, die nur drankommen, wenn ein früherer
# Eintrag kein Bild liefert. Die drei Namen sind am 2026-09-20 gegen den Schlüssel mit
# Guthaben gemessen, nicht geraten (siehe Kommentar oben und design.md).
#
# Das teure Modell steht vorn, weil der kostenlose Boden darunter liegt: ein
# aufgebrauchtes Guthaben kostet damit Bildqualität statt des Bildes. Der mittlere
# Eintrag ist für den Fall da, dass nur das Pro-Modell gerade nicht antwortet, während
# das Konto noch Guthaben hat - genau der Fall, den A21 für die Textmodelle gemessen hat.
_IMAGE_MODEL_CHAIN_DEFAULT = [
    ("gemini", "gemini-3-pro-image"),
    ("gemini", "gemini-3.1-flash-image"),
    ("pollinations", "sana"),
]


def _split_image_chain(raw: str) -> list[tuple[str, str]]:
    """Kommaliste "Anbieter:Modell" zu Paaren. Leerraum weg, Leereinträge weg, Reihenfolge
    erhalten, doppelte Paare weg - die Kette ist eine Reihenfolge, kein Zähler.

    Ein unbekannter Anbieter fällt mit einer Warnung raus, statt den Start zu kosten. Ein
    Eintrag ohne Doppelpunkt ist ein Modellname auf dem Anbieter aus IMAGE_PROVIDER: so
    bleibt eine von Hand geschriebene Kette lesbar, wenn sie nur einen Anbieter benutzt.
    """
    log = logging.getLogger(__name__)
    chain: list[tuple[str, str]] = []
    for part in raw.split(","):
        eintrag = part.strip()
        if not eintrag:
            continue
        if ":" in eintrag:
            anbieter, _, modell = eintrag.partition(":")
            anbieter, modell = anbieter.strip().lower(), modell.strip()
        else:
            anbieter, modell = IMAGE_PROVIDER, eintrag
            log.warning(
                "IMAGE_MODEL_CHAIN-Eintrag %r nennt keinen Anbieter, es gilt %r",
                eintrag, anbieter,
            )
        if not modell:
            log.warning("IMAGE_MODEL_CHAIN-Eintrag %r nennt kein Modell, er fällt weg", eintrag)
            continue
        if anbieter not in _IMAGE_PROVIDERS:
            log.warning(
                "IMAGE_MODEL_CHAIN-Eintrag %r nennt den unbekannten Anbieter %r, er fällt weg",
                eintrag, anbieter,
            )
            continue
        if (anbieter, modell) not in chain:
            chain.append((anbieter, modell))
    return chain


def _resolve_image_chain() -> list[tuple[str, str]]:
    """IMAGE_MODEL_CHAIN gewinnt, wenn gesetzt - wer die Kette ausdrücklich hinschreibt,
    meint genau sie.

    Sonst gilt die Vorgabeliste. Ein **ausdrücklich gesetztes** Paar aus IMAGE_PROVIDER und
    IMAGE_MODEL wandert dabei an die Spitze: eine bestehende .env, die einen Anbieter
    angepinnt hat, behält ihn als bevorzugten und gewinnt die übrigen nur als Rückfall
    darunter. Sind beide gar nicht gesetzt, bleibt die Vorgabeliste in ihrer Reihenfolge;
    ihre Vorgabewerte allein sind keine Ansage. Dieselbe Regel wie bei LLM_MODEL.
    """
    explicit = os.environ.get("IMAGE_MODEL_CHAIN", "")
    if explicit.strip():
        chain = _split_image_chain(explicit)
        if chain:
            return chain
        # Ein Wert nur aus Kommas, Leerraum oder unbekannten Anbietern ist ein Tippfehler
        # und keine Ansage "gar kein Bild" - dafür gibt es IMAGE_ENABLED.
        logging.getLogger(__name__).warning(
            "IMAGE_MODEL_CHAIN nennt keinen brauchbaren Eintrag, es gilt die Vorgabekette"
        )
        return list(_IMAGE_MODEL_CHAIN_DEFAULT)

    chain = list(_IMAGE_MODEL_CHAIN_DEFAULT)
    if os.environ.get("IMAGE_PROVIDER", "").strip() or os.environ.get("IMAGE_MODEL", "").strip():
        pinned = (IMAGE_PROVIDER, IMAGE_MODEL)
        if pinned in chain:
            chain.remove(pinned)
        chain.insert(0, pinned)
    return chain


IMAGE_MODEL_CHAIN = _resolve_image_chain()

# Wie lange ein Eintrag übersprungen wird, der sein Kontingent oder sein Guthaben als
# aufgebraucht gemeldet hat. Eine Stunde wie bei der Textkette: so kostet ein leeres
# Guthaben einen abgewiesenen Aufruf je Stunde statt einen je Import. Der Speicher liegt
# nur im laufenden Prozess, ein Neustart fängt wieder am Kopf der Kette an.
IMAGE_MODEL_COOLDOWN_SECONDS = float(os.environ.get("IMAGE_MODEL_COOLDOWN_SECONDS", "3600"))

# Zeitbudget für die **ganze** Bildstufe eines Imports, nicht für einen Aufruf. Gemessen
# am 2026-09-20: Gemini antwortet in 3 bis 16 s, Pollinations in 35-46 s. 150 s lassen
# einer Kette von drei Einträgen Luft, ohne dass die Push-Meldung (app._publish wartet auf
# diese Stufe) an drei Zeitüberschreitungen hängen kann.
IMAGE_DEADLINE_SECONDS = float(os.environ.get("IMAGE_DEADLINE_SECONDS", "150"))


# ---------------------------------------------------------------------------
# Warteschlange für vorübergehend gescheiterte Importe (A20,
# openspec/changes/add-transient-retry-queue). Alle Werte optional: eine bestehende
# .env läuft unverändert weiter, und die Vorgaben stammen aus design.md.
# ---------------------------------------------------------------------------

# Taktrate der Hintergrundschleife. Bewusst grob gegenüber Verzögerungen, die in
# Minuten gemessen werden: ein Takt kostet eine indizierte SQLite-Abfrage, und "war
# während der Ausfallzeit fällig" löst sich damit von selbst - der erste Takt nach
# dem Start findet den Eintrag.
QUEUE_POLL_SECONDS = int(os.environ.get("QUEUE_POLL_SECONDS", "30"))

# Exponentieller Rückzug in Minuten: base * factor ** (attempts - 1), also mit den
# Vorgaben 2, 6, 18, 54 Minuten. Minuten, nicht Sekunden - ein ausgelasteter Anbieter
# ist in Sekunden nicht weniger ausgelastet.
QUEUE_BACKOFF_BASE_MINUTES = float(os.environ.get("QUEUE_BACKOFF_BASE_MINUTES", "2"))
QUEUE_BACKOFF_FACTOR = float(os.environ.get("QUEUE_BACKOFF_FACTOR", "3"))

# Ab dieser gerechneten Verzögerung wird nicht weiter zurückgezogen, sondern in das
# nächste Nebenzeitfenster gelegt - die Annahme ist, dass Kapazität dort leichter zu
# bekommen ist. Reine Konfiguration, kein gemessenes Modell (design.md, Nicht-Ziele).
QUEUE_OFFPEAK_THRESHOLD_MINUTES = float(os.environ.get("QUEUE_OFFPEAK_THRESHOLD_MINUTES", "60"))

# Nebenzeitfenster als "HH:MM-HH:MM" in **lokaler** Zeit des Containers, damit
# "Nebenzeit" das meint, was der Betreiber gemeint hat. Ein über Mitternacht
# reichendes Fenster ("22:00-04:00") ist erlaubt.
QUEUE_OFFPEAK_WINDOW = os.environ.get("QUEUE_OFFPEAK_WINDOW", "02:00-06:00").strip()


def _parse_offpeak_window(raw: str) -> tuple[int, int]:
    """Fenster als Minuten seit Mitternacht (Start, Ende). Harter Abbruch beim Start
    statt einer stillen Vorgabe: ein vertippter Wert soll auffallen, solange noch
    jemand hinsieht - nicht erst, wenn der erste Import geparkt wird."""
    parts = raw.split("-")
    if len(parts) != 2:
        raise RuntimeError(
            f"QUEUE_OFFPEAK_WINDOW muss die Form HH:MM-HH:MM haben, gelesen wurde {raw!r}"
        )
    bounds = []
    for part in parts:
        piece = part.strip()
        fields = piece.split(":")
        if len(fields) != 2 or not all(f.strip().isdigit() for f in fields):
            raise RuntimeError(
                f"QUEUE_OFFPEAK_WINDOW muss die Form HH:MM-HH:MM haben, gelesen wurde {raw!r}"
            )
        hour, minute = int(fields[0]), int(fields[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise RuntimeError(
                f"QUEUE_OFFPEAK_WINDOW nennt eine Uhrzeit ausserhalb 00:00-23:59: {raw!r}"
            )
        bounds.append(hour * 60 + minute)
    if bounds[0] == bounds[1]:
        raise RuntimeError(
            f"QUEUE_OFFPEAK_WINDOW hat die Länge null, Start und Ende sind gleich: {raw!r}"
        )
    return bounds[0], bounds[1]


QUEUE_OFFPEAK_START_MINUTE, QUEUE_OFFPEAK_END_MINUTE = _parse_offpeak_window(QUEUE_OFFPEAK_WINDOW)

# Aufgabegrenzen. Beide gelten, die zuerst erreichte gewinnt: die Zahl der Versuche
# begrenzt die Kosten, das Alter begrenzt, wie lange "später" heissen darf.
QUEUE_MAX_ATTEMPTS = int(os.environ.get("QUEUE_MAX_ATTEMPTS", "5"))
QUEUE_MAX_AGE_HOURS = float(os.environ.get("QUEUE_MAX_AGE_HOURS", "24"))

# Aufbewahrte Uploads liegen neben der Datenbank, also unter der bereits
# eingehängten Bindung ./data:/data (DESIGN.md §10) - kein neues Volume.
QUEUE_PAYLOAD_DIR = os.environ.get("QUEUE_PAYLOAD_DIR") or str(
    Path(DB_PATH).parent / "queue"
)

# Obergrenze über alle aufbewahrten Uploads zusammen. Wird sie überschritten, scheitert
# der neue Import endgültig, statt den Inhalt eines anderen wartenden Eintrags zu
# verdrängen - verdrängter Inhalt hiesse ein Eintrag, der nie mehr laufen kann.
QUEUE_PAYLOAD_MAX_MB = float(os.environ.get("QUEUE_PAYLOAD_MAX_MB", "100"))
