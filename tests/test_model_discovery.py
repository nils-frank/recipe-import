"""Modellsuche, Probe und Übernahme (Aufgaben 5.1 bis 5.7).

Ohne Netzzugriff (DESIGN.md §12): `requests.get` liefert eine feste Modellliste,
`requests.post` beantwortet die Probe. Geprüft wird die Reihenfolge, der Abgleich mit
der Konfiguration, die Ablehnung einer durchgefallenen Probe, die eine Push-Meldung und
dass **jeder** Fehlschlag die Kette unangetastet lässt.
"""
from __future__ import annotations

import json
import logging

import pytest
import requests

import config
import ha_notify
import llm
import model_chain


def liste(*namen: str, prefix: bool = True) -> dict:
    """Die Form, die der Anbieter liefert. Mit `models/`-Vorsatz, so wie gemessen."""
    return {"data": [{"id": ("models/" + n) if prefix else n} for n in namen]}


class FakeGet:
    def __init__(self, status=200, body=None, exc=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text or json.dumps(body or {})
        self._exc = exc
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self

    def json(self):
        if self._body is None:
            raise ValueError("kein JSON")
        return self._body


class FakePost:
    """Beantwortet die Probe. `antwort` ist entweder ein Inhaltsstring oder eine
    Ausnahme, die stattdessen fliegt."""

    def __init__(self, antwort='{"ok": true}', status=200):
        self.antwort = antwort
        self.status = status
        self.models: list[str] = []

    def __call__(self, *args, **kwargs):
        self.models.append(kwargs["json"]["model"])
        if isinstance(self.antwort, Exception):
            raise self.antwort
        return FakeResponse(self.status, {"choices": [{"message": {"content": self.antwort}}]})


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def suche_an(monkeypatch):
    """`conftest.frische_modellkette` schaltet die Modellsuche für die ganze Testreihe
    ab. Hier ist sie der Gegenstand, also wieder an - der Netzzugriff ist in jedem Test
    dieser Datei ersetzt."""
    monkeypatch.setattr(config, "LLM_MODEL_AUTODISCOVER", True)


@pytest.fixture(autouse=True)
def kein_push(monkeypatch):
    """Vorgabe: keine Meldung geht hinaus. Tests, die sie prüfen, ersetzen das selbst."""
    gesendet: list[tuple] = []
    monkeypatch.setattr(ha_notify, "notify", lambda *a, **k: gesendet.append(a))
    return gesendet


@pytest.fixture
def kette():
    return list(config.LLM_MODEL_CHAIN)


# --------------------------------------------------------------------------
# 5.1 - Liste lesen, Vorsatz abschneiden, nach Zahlen sortieren
# --------------------------------------------------------------------------

def test_neueres_modell_wird_kopf(monkeypatch, kette):
    get = FakeGet(body=liste(*kette, "gemini-4.0-flash"))
    post = FakePost()
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(llm.requests, "post", post)

    model_chain.refresh()

    assert model_chain.candidates() == ["gemini-4.0-flash", *kette]
    assert post.models == ["gemini-4.0-flash"]


def test_zehner_nebenversion_sortiert_ueber_neun(monkeypatch, kette):
    """Aufgabe 5.1: 3.10 steht über 3.9 - deshalb wird nach Zahlen verglichen."""
    get = FakeGet(body=liste(*kette, "gemini-3.9-flash", "gemini-3.10-flash"))
    post = FakePost()
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(llm.requests, "post", post)

    model_chain.refresh()

    assert model_chain.candidates()[0] == "gemini-3.10-flash"


def test_namen_ohne_vorsatz_werden_auch_gelesen(monkeypatch, kette):
    get = FakeGet(body=liste(*kette, "gemini-4.0-flash", prefix=False))
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(llm.requests, "post", FakePost())

    model_chain.refresh()

    assert model_chain.candidates()[0] == "gemini-4.0-flash"


def test_muster_laesst_die_nachbarn_draussen(monkeypatch, kette):
    """Die Namen, die am 2026-09-20 wirklich in derselben Liste standen."""
    get = FakeGet(body=liste(
        *kette,
        "gemini-flash-latest",
        "gemini-3-flash-preview",
        "gemini-9.9-flash-lite",
        "gemini-9.9-flash-image",
        "gemini-9.9-live",
    ))
    monkeypatch.setattr(requests, "get", get)
    post = FakePost()
    monkeypatch.setattr(llm.requests, "post", post)

    model_chain.refresh()

    assert model_chain.candidates() == kette
    assert post.models == []


def test_nichts_neueres_laesst_die_kette_stehen(monkeypatch, kette):
    get = FakeGet(body=liste(*kette))
    post = FakePost()
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(llm.requests, "post", post)

    model_chain.refresh()

    assert model_chain.candidates() == kette
    assert post.models == []


# --------------------------------------------------------------------------
# 5.2 - Abgleich mit der Konfiguration
# --------------------------------------------------------------------------

def test_nicht_gefuehrtes_modell_wird_uebersprungen_und_protokolliert(monkeypatch, caplog, kette):
    ohne_kopf = kette[1:]
    get = FakeGet(body=liste(*ohne_kopf))
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(llm.requests, "post", FakePost())

    with caplog.at_level(logging.WARNING):
        model_chain.refresh()

    assert model_chain.candidates() == ohne_kopf
    assert any(kette[0] in r.getMessage() and "führt" in r.getMessage() for r in caplog.records)


def test_wieder_gefuehrtes_modell_kommt_zurueck(monkeypatch, kette):
    """"Übersprungen, solange die Liste das sagt" - nicht auf Lebenszeit."""
    monkeypatch.setattr(llm.requests, "post", FakePost())

    monkeypatch.setattr(requests, "get", FakeGet(body=liste(*kette[1:])))
    model_chain.refresh()
    assert kette[0] not in model_chain.candidates()

    monkeypatch.setattr(requests, "get", FakeGet(body=liste(*kette)))
    model_chain.refresh()
    assert model_chain.candidates() == kette


# --------------------------------------------------------------------------
# 5.3 - die Probe
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "post",
    [
        FakePost(status=503),
        FakePost(antwort=requests.RequestException("Zeitüberschreitung")),
        FakePost(antwort="kein JSON"),
        FakePost(antwort='{"ok": "ja"}'),   # falscher Typ, passt nicht zum Schema
        FakePost(antwort='{"fertig": true}'),
    ],
    ids=["nicht-200", "zeitueberschreitung", "unlesbar", "falscher-typ", "falsches-feld"],
)
def test_durchgefallene_probe_wird_nicht_uebernommen(monkeypatch, kette, post):
    monkeypatch.setattr(requests, "get", FakeGet(body=liste(*kette, "gemini-4.0-flash")))
    monkeypatch.setattr(llm.requests, "post", post)

    model_chain.refresh()

    assert model_chain.candidates() == kette


def test_durchgefallenes_modell_wird_im_selben_durchlauf_nicht_erneut_geprobt(monkeypatch, kette):
    get = FakeGet(body=liste(*kette, "gemini-4.0-flash"))
    post = FakePost(antwort="kein JSON")
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(llm.requests, "post", post)

    model_chain.refresh()
    model_chain.refresh()

    # Zwei Durchläufe, aber nur eine Probe: der zweite kennt das Urteil schon.
    assert post.models == ["gemini-4.0-flash"]
    assert model_chain.candidates() == kette


def test_probe_faellt_nicht_auf_die_kette_zurueck(monkeypatch, kette):
    """Die Probe pinnt ihr Modell an: sie darf bei einem 429 nicht heimlich das alte
    Modell befragen und dessen Antwort als Beweis nehmen."""
    monkeypatch.setattr(requests, "get", FakeGet(body=liste(*kette, "gemini-4.0-flash")))
    post = FakePost(status=429)
    monkeypatch.setattr(llm.requests, "post", post)

    model_chain.refresh()

    assert set(post.models) == {"gemini-4.0-flash"}
    assert model_chain.candidates() == kette


# --------------------------------------------------------------------------
# 5.4 / 5.5 - Übernahme und Meldung
# --------------------------------------------------------------------------

def test_uebernahme_laesst_die_bisherige_kette_unveraendert_darunter(monkeypatch, kette):
    monkeypatch.setattr(requests, "get", FakeGet(body=liste(*kette, "gemini-4.0-flash")))
    monkeypatch.setattr(llm.requests, "post", FakePost())

    model_chain.refresh()

    assert model_chain.configured() == ["gemini-4.0-flash", *kette]


def test_genau_eine_meldung_je_uebernommenem_modell(monkeypatch, kein_push, kette):
    monkeypatch.setattr(requests, "get", FakeGet(body=liste(*kette, "gemini-4.0-flash")))
    monkeypatch.setattr(llm.requests, "post", FakePost())

    model_chain.refresh()
    model_chain.refresh()

    assert len(kein_push) == 1
    titel, text = kein_push[0][0], kein_push[0][1]
    assert "Sprachmodell" in titel
    assert "gemini-4.0-flash" in text and kette[0] in text


def test_unveraenderter_kopf_meldet_nicht(monkeypatch, kein_push, kette):
    monkeypatch.setattr(requests, "get", FakeGet(body=liste(*kette)))
    monkeypatch.setattr(llm.requests, "post", FakePost())

    model_chain.refresh()

    assert kein_push == []


# --------------------------------------------------------------------------
# 5.6 - abschaltbar
# --------------------------------------------------------------------------

def test_autodiscover_aus_fragt_nichts(monkeypatch, kein_push, kette):
    monkeypatch.setattr(config, "LLM_MODEL_AUTODISCOVER", False)

    def darf_nicht(*a, **k):
        raise AssertionError("Es darf kein Aufruf hinausgehen")

    monkeypatch.setattr(requests, "get", darf_nicht)
    monkeypatch.setattr(llm.requests, "post", darf_nicht)

    model_chain.refresh()

    assert model_chain.candidates() == kette
    assert kein_push == []


# --------------------------------------------------------------------------
# 5.7 - jeder Fehlschlag ist folgenlos
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "get",
    [
        FakeGet(exc=requests.RequestException("keine Verbindung")),
        FakeGet(status=500, text="Interner Fehler"),
        FakeGet(status=200, body=None),                 # kein JSON
        FakeGet(status=200, body={"kein": "data-Feld"}),
        FakeGet(status=200, body={"data": []}),         # leere Liste
        FakeGet(status=200, body={"data": [{"kein_id": 1}]}),
    ],
    ids=["verbindung", "http-500", "unlesbar", "falsche-form", "leer", "eintrag-ohne-id"],
)
def test_fehlschlag_der_suche_laesst_die_kette_stehen(monkeypatch, caplog, kette, get):
    monkeypatch.setattr(requests, "get", get)

    def darf_nicht(*a, **k):
        raise AssertionError("Ohne lesbare Liste darf nichts geprobt werden")

    monkeypatch.setattr(llm.requests, "post", darf_nicht)

    with caplog.at_level(logging.WARNING):
        model_chain.refresh()   # wirft nicht

    assert model_chain.candidates() == kette


def test_kaputtes_muster_laesst_die_kette_stehen(monkeypatch, kette):
    monkeypatch.setattr(config, "LLM_MODEL_PATTERN", "^gemini-((\\d+$")
    monkeypatch.setattr(requests, "get", FakeGet(body=liste(*kette, "gemini-4.0-flash")))
    monkeypatch.setattr(llm.requests, "post", FakePost())

    model_chain.refresh()

    assert model_chain.candidates() == kette
