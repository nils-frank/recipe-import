"""Vorgaben und Prüfung der Warteschlangen-Einstellungen (Aufgabe 1.1, A20).

Wie `test_config_image.py`: `config.py` liest die Umgebung beim Import, und alle
übrigen Module halten dasselbe Modulobjekt. Ein `importlib.reload` mitten in der
Testreihe würde ihnen die Werte unter den Füssen wegziehen, deshalb läuft jeder Fall
hier in einem eigenen Prozess mit einer eigenen Umgebung.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from conftest import SRC_DIR

# Pflichtwerte, sonst bricht `config.require()` den Unterprozess ab.
_BASE_ENV = {
    "MEALIE_TOKEN": "test-platzhalter-mealie-token",
    "HA_TOKEN": "test-platzhalter-ha-token",
    "LLM_API_KEY": "test-platzhalter-llm-key",
    "IMPORT_TOKEN": "test-platzhalter-import-token",
    "HA_NOTIFY_TARGET": "mobile_app_test_device",
}

_NAMES = (
    "QUEUE_POLL_SECONDS",
    "QUEUE_BACKOFF_BASE_MINUTES",
    "QUEUE_BACKOFF_FACTOR",
    "QUEUE_OFFPEAK_THRESHOLD_MINUTES",
    "QUEUE_OFFPEAK_WINDOW",
    "QUEUE_MAX_ATTEMPTS",
    "QUEUE_MAX_AGE_HOURS",
    "QUEUE_PAYLOAD_DIR",
    "QUEUE_PAYLOAD_MAX_MB",
)

_DUMP = (
    "import json, config; "
    f"print(json.dumps({{k: getattr(config, k) for k in {_NAMES!r}}}))"
)


def _run(**overrides) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(SRC_DIR), **_BASE_ENV, **overrides}
    # check=False mit Absicht: der Abbruch bei fehlerhaftem QUEUE_OFFPEAK_WINDOW ist
    # genau das, was hier geprüft wird.
    return subprocess.run(
        [sys.executable, "-c", _DUMP], env=env, capture_output=True, text=True, check=False
    )


def _queue_config(**overrides) -> dict:
    out = _run(**overrides)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.splitlines()[-1])


def test_defaults_without_any_variable_set():
    values = _queue_config()

    assert values["QUEUE_POLL_SECONDS"] == 30
    assert values["QUEUE_BACKOFF_BASE_MINUTES"] == 2
    assert values["QUEUE_BACKOFF_FACTOR"] == 3
    assert values["QUEUE_OFFPEAK_THRESHOLD_MINUTES"] == 60
    assert values["QUEUE_OFFPEAK_WINDOW"] == "02:00-06:00"
    assert values["QUEUE_MAX_ATTEMPTS"] == 5
    assert values["QUEUE_MAX_AGE_HOURS"] == 24
    assert values["QUEUE_PAYLOAD_MAX_MB"] == 100


def test_payload_dir_defaults_next_to_the_database():
    values = _queue_config(DB_PATH="/tmp/beispiel/recipe-import.db")

    assert values["QUEUE_PAYLOAD_DIR"] == "/tmp/beispiel/queue"


def test_explicit_values_win():
    values = _queue_config(
        QUEUE_POLL_SECONDS="5",
        QUEUE_MAX_ATTEMPTS="9",
        QUEUE_OFFPEAK_WINDOW="22:00-04:00",
        QUEUE_PAYLOAD_DIR="/var/tmp/queue",
    )

    assert values["QUEUE_POLL_SECONDS"] == 5
    assert values["QUEUE_MAX_ATTEMPTS"] == 9
    assert values["QUEUE_OFFPEAK_WINDOW"] == "22:00-04:00"
    assert values["QUEUE_PAYLOAD_DIR"] == "/var/tmp/queue"


def test_window_over_midnight_is_allowed():
    values = _queue_config(QUEUE_OFFPEAK_WINDOW="22:00-04:00")

    assert values["QUEUE_OFFPEAK_WINDOW"] == "22:00-04:00"


def test_malformed_window_aborts_the_start_and_names_the_variable():
    """Ein vertippter Wert soll auffallen, solange noch jemand hinsieht - nicht erst,
    wenn der erste Import geparkt wird."""
    for bad in ("02:00", "zwei bis sechs", "25:00-06:00", "02:00-02:00", "02-06"):
        out = _run(QUEUE_OFFPEAK_WINDOW=bad)

        assert out.returncode != 0, f"{bad!r} haette den Start abbrechen muessen"
        assert "QUEUE_OFFPEAK_WINDOW" in out.stderr
