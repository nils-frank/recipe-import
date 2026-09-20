"""Reihenfolge und Gedächtnis der Bildkette (A22, Aufgaben 3.1 bis 3.4).

Ohne Netzzugriff und ohne Schlafen: die Uhr des Moduls (`image_chain._now`) wird ersetzt
und von Hand weitergeschoben, wie in `test_model_chain.py`.
"""
from __future__ import annotations

import config
import image_chain

PRO = ("gemini", "gemini-3-pro-image")
FLASH = ("gemini", "gemini-3.1-flash-image")
FREI = ("pollinations", "sana")


class _Uhr:
    """Monotone Uhr von Hand. `image_chain._now` wird darauf gesetzt."""

    def __init__(self):
        self.jetzt = 1000.0

    def __call__(self) -> float:
        return self.jetzt

    def weiter(self, sekunden: float) -> None:
        self.jetzt += sekunden


def test_frische_kette_gibt_die_konfigurierte_reihenfolge():
    """Aufgabe 3.1. Läuft nach den Tests unten genauso wie davor - die Fixture
    `frische_bildkette` in conftest.py setzt den prozessweiten Zustand je Test zurück
    (Aufgabe 3.5)."""
    assert image_chain.candidates() == [PRO, FLASH, FREI]
    assert image_chain.configured() == [PRO, FLASH, FREI]


def test_erschoepfter_kandidat_faellt_bis_zum_fristende_weg(monkeypatch):
    """Aufgabe 3.2: vor der Frist weg, danach wieder da - ohne zu schlafen."""
    uhr = _Uhr()
    monkeypatch.setattr(image_chain, "_now", uhr)

    image_chain.mark_exhausted(PRO)
    assert image_chain.candidates() == [FLASH, FREI]

    uhr.weiter(config.IMAGE_MODEL_COOLDOWN_SECONDS - 1)
    assert image_chain.candidates() == [FLASH, FREI]

    uhr.weiter(2)
    assert image_chain.candidates() == [PRO, FLASH, FREI]


def test_erschoepfung_haelt_die_reihenfolge_der_uebrigen(monkeypatch):
    uhr = _Uhr()
    monkeypatch.setattr(image_chain, "_now", uhr)

    image_chain.mark_exhausted(FLASH)

    assert image_chain.candidates() == [PRO, FREI]


def test_unbekanntes_modell_kommt_nie_wieder(monkeypatch):
    """Aufgabe 3.3: unabhängig von jeder Uhr."""
    uhr = _Uhr()
    monkeypatch.setattr(image_chain, "_now", uhr)

    image_chain.mark_unknown(PRO)
    assert image_chain.candidates() == [FLASH, FREI]

    uhr.weiter(config.IMAGE_MODEL_COOLDOWN_SECONDS * 10)
    assert image_chain.candidates() == [FLASH, FREI]


def test_leere_kandidatenliste_ist_ein_zustand_keine_ausnahme(monkeypatch):
    """Aufgabe 3.4: der Aufrufer muss das deuten können, nicht abstürzen."""
    monkeypatch.setattr(image_chain, "_now", _Uhr())

    for kandidat in (PRO, FLASH, FREI):
        image_chain.mark_exhausted(kandidat)

    assert image_chain.candidates() == []
    # Die konfigurierte Kette steht weiter, nur gesperrt - daran unterscheidet der
    # Aufrufer "später nochmal" von "falsch konfiguriert".
    assert image_chain.configured() == [PRO, FLASH, FREI]


def test_zwei_anbieter_mit_demselben_modellnamen_sind_zwei_kandidaten(monkeypatch):
    monkeypatch.setattr(config, "IMAGE_MODEL_CHAIN", [("gemini", "sana"), ("pollinations", "sana")])
    image_chain.reset()

    image_chain.mark_unknown(("gemini", "sana"))

    assert image_chain.candidates() == [("pollinations", "sana")]
