"""Reihenfolge, Sperrfristen und Gedächtnis der Modellkette (Aufgaben 3.1 bis 3.4).

Ohne Netzzugriff und ohne Schlafen (DESIGN.md §12): die Uhr des Moduls ist der Haken
`model_chain._now`, den jeder Test durch einen Zähler ersetzt und von Hand weiterstellt.
"""
from __future__ import annotations

import pytest

import config
import model_chain


class Uhr:
    """Monotone Uhr von Hand. `vor(n)` schiebt sie um n Sekunden weiter."""

    def __init__(self) -> None:
        self.jetzt = 1000.0

    def __call__(self) -> float:
        return self.jetzt

    def vor(self, sekunden: float) -> None:
        self.jetzt += sekunden


@pytest.fixture
def uhr(monkeypatch):
    u = Uhr()
    monkeypatch.setattr(model_chain, "_now", u)
    model_chain.reset()
    yield u
    model_chain.reset()


def test_frische_kette_ist_die_konfigurierte(uhr):
    """Aufgabe 3.1."""
    assert model_chain.candidates() == list(config.LLM_MODEL_CHAIN)
    assert model_chain.head() == config.LLM_MODEL_CHAIN[0]


def test_erschoepftes_modell_faellt_bis_zur_frist_weg(uhr):
    """Aufgabe 3.2: vor der Frist weg, nach der Frist wieder an seinem Platz."""
    kopf = config.LLM_MODEL_CHAIN[0]
    model_chain.mark_exhausted(kopf)

    assert kopf not in model_chain.candidates()
    assert model_chain.candidates() == list(config.LLM_MODEL_CHAIN[1:])
    assert model_chain.head() == config.LLM_MODEL_CHAIN[1]

    # Kurz vor der Frist noch gesperrt.
    uhr.vor(config.LLM_MODEL_COOLDOWN_SECONDS - 1)
    assert kopf not in model_chain.candidates()

    # Danach wieder da, und zwar an seiner konfigurierten Stelle, nicht hinten.
    uhr.vor(2)
    assert model_chain.candidates() == list(config.LLM_MODEL_CHAIN)


def test_unbekanntes_modell_kommt_nie_zurueck(uhr):
    """Aufgabe 3.3: kein Zeitwert, keine Rückkehr - auch nach sehr langer Zeit nicht."""
    kopf = config.LLM_MODEL_CHAIN[0]
    model_chain.mark_unknown(kopf)

    assert kopf not in model_chain.candidates()
    uhr.vor(config.LLM_MODEL_COOLDOWN_SECONDS * 1000)
    assert kopf not in model_chain.candidates()


def test_leere_kette_ist_ein_normaler_zustand(uhr):
    """Aufgabe 3.4: alles gesperrt ergibt eine leere Liste, keine Ausnahme."""
    for model in config.LLM_MODEL_CHAIN:
        model_chain.mark_exhausted(model)

    assert model_chain.candidates() == []
    assert model_chain.head() is None

    # Und nach der Frist ist wieder die volle Kette da.
    uhr.vor(config.LLM_MODEL_COOLDOWN_SECONDS + 1)
    assert model_chain.candidates() == list(config.LLM_MODEL_CHAIN)


def test_configured_head_ignoriert_sperrfristen(uhr):
    """`configured_head()` nennt das normalerweise genutzte Modell, damit die
    Modellsuche eine zufällig gesperrte Spitze nicht für veraltet hält."""
    kopf = config.LLM_MODEL_CHAIN[0]
    model_chain.mark_exhausted(kopf)

    assert model_chain.head() != kopf
    assert model_chain.configured_head() == kopf


def test_zweimal_melden_ist_harmlos(uhr):
    """Mehrere Hintergrundimporte können dasselbe Modell gleichzeitig melden."""
    kopf = config.LLM_MODEL_CHAIN[0]
    model_chain.mark_exhausted(kopf)
    model_chain.mark_unknown(kopf)
    model_chain.mark_exhausted(kopf)
    model_chain.mark_unknown(kopf)

    assert kopf not in model_chain.candidates()
