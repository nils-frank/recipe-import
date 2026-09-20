"""Gemeinsame Testinfrastruktur für recipe-import (DESIGN.md §12).

`config.py` bricht beim Import hart ab, wenn eine Pflicht-Umgebungsvariable fehlt
(`config.require`, DESIGN.md §3). Deshalb werden Platzhalterwerte hier gesetzt, bevor
irgendein Testmodul zum ersten Mal `config.py` oder ein davon abhängiges Modul
importiert. Kein echtes Secret - reine Platzhalter für den Testlauf.

`src/` liegt nicht auf dem Standard-Modulsuchpfad; die Module importieren sich
gegenseitig flach (`from schema import Recipe`, nicht `from src.schema import ...`),
deshalb wird `src/` hier vorn in `sys.path` eingehängt statt ein Package-Layout zu
erfinden, das es im Code nicht gibt.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).parent
FIXTURES_DIR = TESTS_DIR / "fixtures"
SRC_DIR = TESTS_DIR.parent / "src"

sys.path.insert(0, str(SRC_DIR))

os.environ.setdefault("MEALIE_TOKEN", "test-platzhalter-mealie-token")
os.environ.setdefault("HA_TOKEN", "test-platzhalter-ha-token")
os.environ.setdefault("LLM_API_KEY", "test-platzhalter-llm-key")
os.environ.setdefault("IMPORT_TOKEN", "test-platzhalter-import-token")
os.environ.setdefault("HA_NOTIFY_TARGET", "mobile_app_test_device")

import pytest  # noqa: E402 - erst nach sys.path-Anpassung, siehe oben


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Frische SQLite-Datei je Test statt der echten DB_PATH aus config.py - Tests
    dürfen sich nicht gegenseitig über den Datenbankinhalt beeinflussen (DESIGN.md §12,
    Punkte 5 und 6)."""
    import store

    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "recipe-import-test.db"))
    store.init()
    return store


@pytest.fixture(autouse=True)
def frische_modellkette(monkeypatch):
    """Die Modellkette (A21) merkt sich prozessweit, welches Modell erschöpft oder
    unbekannt ist - genau dafür ist sie da. In einer Testreihe hiesse das, dass ein
    Test, der ein 429 nachstellt, dem nächsten die Kette leerräumt. Deshalb fängt jeder
    Test mit der konfigurierten Kette an.

    Die Modellsuche ist dabei aus, wie die Namens- und die Bildstufe: sie liest beim
    Start die Modellliste des Anbieters, und DESIGN.md §12 verlangt eine Testreihe
    "ohne jeden Netzzugriff". Wer sie prüfen will, schaltet sie ausdrücklich ein (siehe
    test_model_discovery.py)."""
    import config
    import model_chain

    monkeypatch.setattr(config, "LLM_MODEL_AUTODISCOVER", False)
    model_chain.reset()
    yield
    model_chain.reset()


@pytest.fixture(autouse=True)
def naming_off(monkeypatch):
    """Die Namensstufe (A18) ist im Dienst standardmässig an und ruft dabei das
    Sprachmodell. Für die Testreihe gilt DESIGN.md §12 "ohne jeden Netzzugriff", deshalb
    ist sie überall aus - ein Test, der sie prüfen will, schaltet sie ausdrücklich ein
    (siehe test_naming.py). So bleibt auch sichtbar, dass alle übrigen Zusicherungen
    unabhängig von dieser Stufe gelten."""
    import config

    monkeypatch.setattr(config, "NAMING_ENABLED", False)


@pytest.fixture(autouse=True)
def image_off(monkeypatch):
    """Die Bildstufe (A19) ist im Dienst standardmässig an und ruft dabei ein
    Bildmodell. Wie bei der Namensstufe gilt für die Testreihe DESIGN.md §12 "ohne jeden
    Netzzugriff", deshalb ist sie überall aus - ein Test, der sie prüfen will, schaltet
    sie ausdrücklich ein (siehe test_image.py). So bleibt sichtbar, dass alle übrigen
    Zusicherungen unabhängig von dieser Stufe gelten."""
    import config

    monkeypatch.setattr(config, "IMAGE_ENABLED", False)


@pytest.fixture(autouse=True)
def recipe_still_in_mealie(monkeypatch):
    """`app._notify_if_done` fragt bei einem `done`-Eintrag zuerst bei Mealie nach, ob
    der Slug dort noch existiert. Das ist ein HTTP-Aufruf, und DESIGN.md §12 verlangt
    eine Testreihe ohne jeden Netzzugriff - deshalb gilt hier überall "Rezept ist noch
    da". Der Test zum gelöschten Rezept (test_idempotency.py) setzt das ausdrücklich
    um."""
    import mealie_client

    monkeypatch.setattr(mealie_client, "recipe_exists", lambda slug: True)
