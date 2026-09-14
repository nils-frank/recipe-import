"""Konfiguration ausschliesslich über Umgebungsvariablen. Kein Modul liest
Umgebungsvariablen ausserhalb dieser Datei. `os.environ.get` mit Vorgabewert, plus
`require()` für Pflichtwerte, die beim Start hart abbricht statt später mit einem
unklaren Fehler zu scheitern.
"""
from __future__ import annotations

import os


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

# Wechselt später auf den LiteLLM-Proxy, siehe DESIGN.md §3. LLM_MODEL bewusst fest
# verdrahtet statt als wandernder Alias ("latest" o.ä.), damit ein Modellwechsel eine
# bewusste Änderung ist.
LLM_BASE_URL = os.environ.get(
    "LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
)
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3.6-flash")
LLM_API_KEY = require("LLM_API_KEY")

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
