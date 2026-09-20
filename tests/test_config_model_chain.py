"""Auflösung der Modellkette aus der Umgebung (Aufgaben 2.1 bis 2.3).

Gleiche Bauart wie `test_config_image.py`: `config.py` liest die Umgebung beim Import,
und alle übrigen Module halten dasselbe Modulobjekt - ein `importlib.reload` mitten in
der Testreihe zöge ihnen die Werte unter den Füssen weg. Deshalb läuft jeder Fall in
einem eigenen Prozess mit einer eigenen Umgebung.
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

_KEYS = (
    "LLM_MODEL",
    "LLM_MODEL_CHAIN",
    "LLM_MODEL_COOLDOWN_SECONDS",
    "LLM_MODEL_AUTODISCOVER",
    "LLM_MODEL_PATTERN",
    "LLM_MODEL_REFRESH_SECONDS",
)

_DUMP = (
    "import json, config; "
    f"print(json.dumps({{k: getattr(config, k) for k in {_KEYS!r}}}))"
)

# Die gemessene Vorgabekette, siehe design.md (Context, Probe vom 2026-09-20).
_DEFAULT_CHAIN = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
]


def _config(**overrides) -> dict:
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(SRC_DIR), **_BASE_ENV, **overrides}
    out = subprocess.run(
        [sys.executable, "-c", _DUMP], env=env, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout.splitlines()[-1])


def test_vorgaben_ohne_umgebung():
    """Aufgabe 2.1: jede neue Variable hat die Vorgabe aus design.md."""
    cfg = _config()
    assert cfg["LLM_MODEL_CHAIN"] == _DEFAULT_CHAIN
    assert cfg["LLM_MODEL_COOLDOWN_SECONDS"] == 3600
    assert cfg["LLM_MODEL_AUTODISCOVER"] is True
    assert cfg["LLM_MODEL_PATTERN"] == r"^gemini-(\d+)\.(\d+)-flash$"
    assert cfg["LLM_MODEL_REFRESH_SECONDS"] == 86400
    # LLM_MODEL behält seinen bisherigen Vorgabewert und seine Bedeutung (Aufgabe 2.3).
    assert cfg["LLM_MODEL"] == "gemini-3.6-flash"


def test_ausdrueckliche_kette_gewinnt():
    cfg = _config(LLM_MODEL_CHAIN="alpha,beta")
    assert cfg["LLM_MODEL_CHAIN"] == ["alpha", "beta"]


def test_kette_ohne_leerraum_und_ohne_doppelte():
    """Leerraum, Leereinträge und Wiederholungen fallen weg, die Reihenfolge bleibt."""
    cfg = _config(LLM_MODEL_CHAIN="  alpha , , beta ,alpha,  gamma  ")
    assert cfg["LLM_MODEL_CHAIN"] == ["alpha", "beta", "gamma"]


def test_leere_kette_faellt_auf_die_vorgabe_zurueck():
    """Ein Wert aus nur Kommas ist ein Tippfehler, keine Ansage "gar kein Modell"."""
    cfg = _config(LLM_MODEL_CHAIN=" , , ")
    assert cfg["LLM_MODEL_CHAIN"] == _DEFAULT_CHAIN


def test_angepinntes_modell_ausserhalb_der_vorgabe_wird_kopf():
    """Aufgabe 2.3: eine bestehende .env mit nur LLM_MODEL behält ihr Modell als Kopf
    und gewinnt die Vorgabeliste als Rückfall darunter."""
    cfg = _config(LLM_MODEL="gemini-4.0-flash")
    assert cfg["LLM_MODEL_CHAIN"] == ["gemini-4.0-flash", *_DEFAULT_CHAIN]
    assert cfg["LLM_MODEL"] == "gemini-4.0-flash"


def test_angepinntes_modell_aus_der_vorgabe_wird_nach_vorn_gezogen():
    """Auch ein Name, der ohnehin in der Liste steht, wird durch das Anpinnen zum Kopf -
    er steht dann nur einmal in der Kette."""
    cfg = _config(LLM_MODEL="gemini-3.5-flash")
    assert cfg["LLM_MODEL_CHAIN"] == [
        "gemini-3.5-flash",
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
    ]


def test_ausdrueckliche_kette_schlaegt_angepinntes_modell():
    cfg = _config(LLM_MODEL="gemini-4.0-flash", LLM_MODEL_CHAIN="alpha,beta")
    assert cfg["LLM_MODEL_CHAIN"] == ["alpha", "beta"]


def test_autodiscover_abschaltbar():
    for wert in ("false", "0", "no", "off", "  FALSE  "):
        assert _config(LLM_MODEL_AUTODISCOVER=wert)["LLM_MODEL_AUTODISCOVER"] is False
    assert _config(LLM_MODEL_AUTODISCOVER="true")["LLM_MODEL_AUTODISCOVER"] is True


def test_zahlenwerte_aus_der_umgebung():
    cfg = _config(LLM_MODEL_COOLDOWN_SECONDS="90", LLM_MODEL_REFRESH_SECONDS="300")
    assert cfg["LLM_MODEL_COOLDOWN_SECONDS"] == 90
    assert cfg["LLM_MODEL_REFRESH_SECONDS"] == 300
