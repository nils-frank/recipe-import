"""Vorgaben der Bildstufe je Anbieter (Aufgabe 9.1).

`config.py` liest die Umgebung beim Import, und alle übrigen Module halten dasselbe
Modulobjekt - ein `importlib.reload` mitten in der Testreihe würde ihnen die Werte unter
den Füssen wegziehen. Deshalb läuft jeder Fall hier in einem eigenen Prozess mit einer
eigenen Umgebung.
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

_DUMP = (
    "import json, config; "
    "print(json.dumps({k: getattr(config, k) for k in "
    "('IMAGE_PROVIDER', 'IMAGE_MODEL', 'IMAGE_BASE_URL', 'IMAGE_API_KEY', 'LLM_BASE_URL')}))"
)


def _image_config(**overrides) -> dict:
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(SRC_DIR), **_BASE_ENV, **overrides}
    out = subprocess.run(
        [sys.executable, "-c", _DUMP], env=env, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout.splitlines()[-1])


def test_defaults_to_pollinations_without_a_key():
    """Der Schlüssel des Textmodells darf nicht zu einer anderen Firma wandern."""
    values = _image_config()

    assert values["IMAGE_PROVIDER"] == "pollinations"
    assert values["IMAGE_MODEL"] == "sana"
    assert values["IMAGE_BASE_URL"] == "https://image.pollinations.ai"
    assert values["IMAGE_API_KEY"] == ""


def test_openai_provider_falls_back_to_the_text_model():
    values = _image_config(IMAGE_PROVIDER="openai")

    assert values["IMAGE_PROVIDER"] == "openai"
    assert values["IMAGE_BASE_URL"] == values["LLM_BASE_URL"]
    assert values["IMAGE_API_KEY"] == "test-platzhalter-llm-key"


def test_explicit_values_win_over_both_defaults():
    values = _image_config(
        IMAGE_PROVIDER="pollinations",
        IMAGE_MODEL="flux",
        IMAGE_BASE_URL="https://bilder.example",
        IMAGE_API_KEY="test-platzhalter-token",
    )

    assert values["IMAGE_MODEL"] == "flux"
    assert values["IMAGE_BASE_URL"] == "https://bilder.example"
    assert values["IMAGE_API_KEY"] == "test-platzhalter-token"


def test_unknown_provider_falls_back_instead_of_failing_the_start():
    """Ein Tippfehler in der .env darf den Dienst nicht am Start hindern - die Bildstufe
    ist die unwichtigste Stufe des Ablaufs."""
    values = _image_config(IMAGE_PROVIDER="Pollinationz")

    assert values["IMAGE_PROVIDER"] == "pollinations"
    assert values["IMAGE_API_KEY"] == ""
