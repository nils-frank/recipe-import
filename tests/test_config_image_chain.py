"""Auflösung der Bildkette aus der Umgebung (A22, Aufgaben 2.1 bis 2.4).

Gleiche Bauart wie `test_config_image.py` und `test_config_model_chain.py`: `config.py`
liest die Umgebung beim Import, und alle übrigen Module halten dasselbe Modulobjekt - ein
`importlib.reload` mitten in der Testreihe zöge ihnen die Werte unter den Füssen weg.
Deshalb läuft jeder Fall in einem eigenen Prozess mit einer eigenen Umgebung.
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
    "IMAGE_PROVIDER",
    "IMAGE_MODEL",
    "IMAGE_MODEL_CHAIN",
    "IMAGE_MODEL_COOLDOWN_SECONDS",
    "IMAGE_DEADLINE_SECONDS",
    "IMAGE_BASE_URL",
    "IMAGE_API_KEY",
    "IMAGE_ENDPOINTS",
    "LLM_BASE_URL",
)

_DUMP = (
    "import json, config; "
    f"print(json.dumps({{k: getattr(config, k) for k in {_KEYS!r}}}))"
)

# Die gemessene Vorgabekette, siehe design.md (Context, Probe vom 2026-09-20). JSON kennt
# keine Tupel, deshalb Listen.
_DEFAULT_CHAIN = [
    ["gemini", "gemini-3-pro-image"],
    ["gemini", "gemini-3.1-flash-image"],
    ["pollinations", "sana"],
]


def _config(**overrides) -> dict:
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(SRC_DIR), **_BASE_ENV, **overrides}
    out = subprocess.run(
        [sys.executable, "-c", _DUMP], env=env, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout.splitlines()[-1])


# --- Aufgabe 2.1: Vorgaben ------------------------------------------------------------


def test_vorgaben_ohne_umgebung():
    cfg = _config()

    assert cfg["IMAGE_MODEL_CHAIN"] == _DEFAULT_CHAIN
    assert cfg["IMAGE_MODEL_COOLDOWN_SECONDS"] == 3600.0
    assert cfg["IMAGE_DEADLINE_SECONDS"] == 150.0


def test_eigene_werte_fuer_sperrfrist_und_zeitbudget():
    cfg = _config(IMAGE_MODEL_COOLDOWN_SECONDS="60", IMAGE_DEADLINE_SECONDS="45.5")

    assert cfg["IMAGE_MODEL_COOLDOWN_SECONDS"] == 60.0
    assert cfg["IMAGE_DEADLINE_SECONDS"] == 45.5


# --- Aufgabe 2.2: Zerlegung der Kette -------------------------------------------------


def test_leerraum_und_leereintraege_fallen_weg():
    cfg = _config(IMAGE_MODEL_CHAIN="  gemini : gemini-3-pro-image ,, , pollinations:sana ")

    assert cfg["IMAGE_MODEL_CHAIN"] == [
        ["gemini", "gemini-3-pro-image"],
        ["pollinations", "sana"],
    ]


def test_doppelte_eintraege_fallen_weg_die_reihenfolge_bleibt():
    cfg = _config(
        IMAGE_MODEL_CHAIN="pollinations:sana, gemini:gemini-3-pro-image, pollinations:sana"
    )

    assert cfg["IMAGE_MODEL_CHAIN"] == [
        ["pollinations", "sana"],
        ["gemini", "gemini-3-pro-image"],
    ]


def test_unbekannter_anbieter_faellt_weg_statt_den_start_zu_kosten():
    cfg = _config(IMAGE_MODEL_CHAIN="pollinationz:sana, gemini:gemini-3-pro-image")

    assert cfg["IMAGE_MODEL_CHAIN"] == [["gemini", "gemini-3-pro-image"]]


def test_eintrag_ohne_doppelpunkt_gilt_fuer_den_anbieter_aus_image_provider():
    cfg = _config(IMAGE_PROVIDER="pollinations", IMAGE_MODEL_CHAIN="flux")

    assert cfg["IMAGE_MODEL_CHAIN"] == [["pollinations", "flux"]]


def test_eintrag_ohne_modell_faellt_weg():
    cfg = _config(IMAGE_MODEL_CHAIN="gemini:, pollinations:sana")

    assert cfg["IMAGE_MODEL_CHAIN"] == [["pollinations", "sana"]]


def test_kette_ganz_ohne_brauchbaren_eintrag_faellt_auf_die_vorgabe_zurueck():
    """Ein Tippfehler ist keine Ansage "gar kein Bild" - dafür gibt es IMAGE_ENABLED."""
    cfg = _config(IMAGE_MODEL_CHAIN=" , , ")

    assert cfg["IMAGE_MODEL_CHAIN"] == _DEFAULT_CHAIN


# --- Aufgabe 2.3: Vorrangregel --------------------------------------------------------


def test_ausdrueckliche_kette_gewinnt_gegen_den_angepinnten_anbieter():
    cfg = _config(
        IMAGE_PROVIDER="pollinations",
        IMAGE_MODEL="sana",
        IMAGE_MODEL_CHAIN="gemini:gemini-3.1-flash-image",
    )

    assert cfg["IMAGE_MODEL_CHAIN"] == [["gemini", "gemini-3.1-flash-image"]]


def test_angepinntes_paar_aus_der_vorgabekette_wandert_nach_vorn():
    cfg = _config(IMAGE_PROVIDER="gemini", IMAGE_MODEL="gemini-3.1-flash-image")

    assert cfg["IMAGE_MODEL_CHAIN"] == [
        ["gemini", "gemini-3.1-flash-image"],
        ["gemini", "gemini-3-pro-image"],
        ["pollinations", "sana"],
    ]


def test_angepinntes_paar_ausserhalb_der_vorgabekette_kommt_davor():
    """Eine bestehende .env, die Pollinations angepinnt hat, behält es als bevorzugt und
    gewinnt die Gemini-Einträge nur als Rückfall darunter."""
    cfg = _config(IMAGE_PROVIDER="pollinations", IMAGE_MODEL="flux")

    assert cfg["IMAGE_MODEL_CHAIN"] == [["pollinations", "flux"], *_DEFAULT_CHAIN]


# --- Aufgabe 2.4: Adresse und Schlüssel je Anbieter -----------------------------------


def test_pollinations_bekommt_nie_den_schluessel_des_textmodells():
    cfg = _config()
    endpoints = cfg["IMAGE_ENDPOINTS"]

    assert endpoints["pollinations"] == ["https://image.pollinations.ai", ""]
    assert _BASE_ENV["LLM_API_KEY"] not in json.dumps(endpoints["pollinations"])


def test_gemini_adresse_wird_aus_llm_base_url_abgeleitet():
    cfg = _config()
    basis, schluessel = cfg["IMAGE_ENDPOINTS"]["gemini"]

    assert cfg["LLM_BASE_URL"].endswith("/openai")
    assert basis == cfg["LLM_BASE_URL"].removesuffix("/openai")
    assert schluessel == "test-platzhalter-llm-key"


def test_proxy_ohne_openai_suffix_bleibt_unveraendert():
    cfg = _config(LLM_BASE_URL="https://proxy.example/v1beta/")
    basis, _ = cfg["IMAGE_ENDPOINTS"]["gemini"]

    assert basis == "https://proxy.example/v1beta"


def test_ueberschreibung_gilt_nur_fuer_den_angepinnten_anbieter():
    cfg = _config(
        IMAGE_PROVIDER="pollinations",
        IMAGE_BASE_URL="https://bilder.example",
        IMAGE_API_KEY="test-platzhalter-token",
    )
    endpoints = cfg["IMAGE_ENDPOINTS"]

    assert endpoints["pollinations"] == ["https://bilder.example", "test-platzhalter-token"]
    assert endpoints["gemini"][0] == cfg["LLM_BASE_URL"].removesuffix("/openai")
    assert endpoints["gemini"][1] == "test-platzhalter-llm-key"


def test_gemini_als_angepinnter_anbieter_nimmt_die_native_flaeche():
    cfg = _config(IMAGE_PROVIDER="gemini")

    assert cfg["IMAGE_MODEL"] == "gemini-3-pro-image"
    assert cfg["IMAGE_BASE_URL"] == cfg["LLM_BASE_URL"].removesuffix("/openai")
    assert cfg["IMAGE_API_KEY"] == "test-platzhalter-llm-key"
