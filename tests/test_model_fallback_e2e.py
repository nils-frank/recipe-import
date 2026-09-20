"""Der Modellwechsel durch den ganzen Import hindurch (Aufgaben 7.1 und 7.2).

Nicht `_post` allein, sondern der Weg, den ein Mensch auslöst: URL hinein, Rezept in
Mealie, eine Push-Meldung hinaus. Die Gegenstelle ist ein erfundener Anbieter, der je
Modellnamen anders antwortet - genau das, was am 2026-09-20 gemessen wurde, als
gemini-3.6-flash 429 antwortete und zwei andere Modelle 200.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import app as app_module
import config
import llm
from classify import url_hash
from sources import SourceResult

URL = "https://example.org/pfannkuchen"

REZEPT_JSON = json.dumps({
    "name": "Pfannkuchen",
    "recipeIngredient": ["200 g Mehl", "300 ml Milch", "2 Eier"],
    "recipeInstructions": ["Verrühren.", "Backen."],
    "recipeYield": None,
    "totalTime": None,
    "description": None,
    "recipeCategory": [],
    "url": None,
})

QUOTA_BODY = {
    "error": {"code": 429, "message": "You exceeded your current quota", "status": "RESOURCE_EXHAUSTED"}
}


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


@pytest.fixture
def notes(monkeypatch):
    """Alle Push-Meldungen dieses Tests, in der Reihenfolge ihres Versands."""
    sent: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        app_module.ha_notify, "notify", lambda title, message, link=None: sent.append((title, message, link))
    )
    return sent


@pytest.fixture
def payload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QUEUE_PAYLOAD_DIR", str(tmp_path / "queue"))
    return tmp_path / "queue"


@pytest.fixture
def quelle(monkeypatch):
    """Mealie kann die Seite nicht selbst schaben, der Text geht ans Sprachmodell."""
    monkeypatch.setattr(app_module.mealie_client, "import_url", lambda url: None)
    monkeypatch.setattr(
        app_module.site,
        "fetch",
        lambda url: SourceResult(
            recipe=None, text="Pfannkuchen: Mehl, Milch, Eier", title=None, stage="text"
        ),
    )


@pytest.fixture
def mealie(monkeypatch):
    angelegt: list[dict] = []
    monkeypatch.setattr(app_module.mealie_client, "create_from_jsonld",
                        lambda data: angelegt.append(data) or "pfannkuchen")
    monkeypatch.setattr(app_module.mealie_client, "set_tags", lambda slug, tags: None)
    monkeypatch.setattr(app_module.mealie_client, "recipe_link", lambda slug: f"https://mealie/{slug}")
    return angelegt


def _anbieter(monkeypatch, plan: dict, default: FakeResponse):
    """Antwortet je Modellname und merkt sich, wer gefragt wurde."""
    rest = {k: list(v) for k, v in plan.items()}
    gefragt: list[str] = []

    def post(*args, **kwargs):
        model = kwargs["json"]["model"]
        gefragt.append(model)
        folge = rest.get(model)
        if folge:
            return folge.pop(0)
        return default

    monkeypatch.setattr(llm.requests, "post", post)
    return gefragt


def test_erstes_modell_ohne_kontingent_der_import_gelingt_trotzdem(
    monkeypatch, isolated_store, notes, payload_dir, quelle, mealie
):
    """7.1: das bevorzugte Modell antwortet nur noch 429 - der Mensch merkt nichts
    davon ausser dass es etwas länger dauert."""
    kette = list(config.LLM_MODEL_CHAIN)
    gefragt = _anbieter(
        monkeypatch,
        {
            kette[0]: [FakeResponse(429, QUOTA_BODY), FakeResponse(429, QUOTA_BODY)],
            kette[1]: [FakeResponse(200, {"choices": [{"message": {"content": REZEPT_JSON}}]})],
        },
        default=FakeResponse(429, QUOTA_BODY),
    )

    asyncio.run(app_module._process_import(URL))

    zeile = isolated_store.find(url_hash(URL))
    assert zeile["status"] == "done"
    assert len(mealie) == 1
    assert mealie[0]["name"] == "Pfannkuchen"
    assert notes == [("Rezept angelegt", "Pfannkuchen", "https://mealie/pfannkuchen")]

    # Geantwortet hat das Rückfallmodell, und das erschöpfte wurde zweimal gefragt.
    assert gefragt == [kette[0], kette[0], kette[1]]


def test_zweiter_import_faengt_beim_rueckfallmodell_an(
    monkeypatch, isolated_store, notes, payload_dir, quelle, mealie
):
    """Die Sperrfrist wirkt über Importe hinweg: der zweite Import kostet keinen
    abgelehnten Aufruf mehr."""
    kette = list(config.LLM_MODEL_CHAIN)
    gutes = FakeResponse(200, {"choices": [{"message": {"content": REZEPT_JSON}}]})
    gefragt = _anbieter(
        monkeypatch,
        {kette[0]: [FakeResponse(429, QUOTA_BODY), FakeResponse(429, QUOTA_BODY)]},
        default=gutes,
    )

    asyncio.run(app_module._process_import(URL))
    gefragt.clear()
    asyncio.run(app_module._process_import("https://example.org/anderes-rezept"))

    assert gefragt == [kette[1]]


def test_jedes_modell_ohne_kontingent_wird_geparkt(
    monkeypatch, isolated_store, notes, payload_dir, quelle, mealie
):
    """7.2, erster Teil: ist die ganze Kette erschöpft, gilt genau das Verhalten, das
    ein einzelnes erschöpftes Modell vorher hatte.

    Seit A20 (Warteschlange) heisst das nicht mehr "sofort gescheitert", sondern
    "geparkt und automatisch wiederholt" - genau das, was `LlmOverloadedError` auslöst.
    Kein neuer Fehlerfall, keine neue Meldung.
    """
    kette = list(config.LLM_MODEL_CHAIN)
    gefragt = _anbieter(monkeypatch, {}, default=FakeResponse(429, QUOTA_BODY))

    asyncio.run(app_module._process_import(URL))

    zeile = isolated_store.find(url_hash(URL))
    assert zeile["status"] == "queued"
    assert mealie == []
    assert notes == [
        (app_module.QUEUED_TITLE, app_module._TRANSIENT_MESSAGES["LlmOverloadedError"], None)
    ]

    # Jedes Modell wurde genau zweimal gefragt, keines ausgelassen.
    assert gefragt == [m for m in kette for _ in (1, 2)]


def test_erschoepfte_kette_ohne_warteschlange_traegt_den_wortlaut_aus_paragraph_7(
    monkeypatch, isolated_store, notes, payload_dir, quelle, mealie
):
    """7.2, zweiter Teil: ohne Wiederholung endet derselbe Fall im unveränderten
    Wortlaut aus DESIGN.md §7 - die Änderung führt keinen neuen Text ein."""
    monkeypatch.setattr(config, "QUEUE_MAX_ATTEMPTS", 0)   # gar kein Versuch mehr
    _anbieter(monkeypatch, {}, default=FakeResponse(429, QUOTA_BODY))

    asyncio.run(app_module._process_import(URL))

    zeile = isolated_store.find(url_hash(URL))
    assert zeile["status"] == "failed"
    assert mealie == []
    assert notes == [("Import fehlgeschlagen", app_module.GIVE_UP_MESSAGE, None)]

    # Und der Wortlaut, den die Ausnahme selbst trägt, ist der aus §7 - unverändert.
    assert app_module._describe_source_error(llm.LlmOverloadedError("429")) == (
        "Das Sprachmodell ist gerade überlastet. Bitte später erneut teilen."
    )


def test_unbekannte_modelle_scheitern_endgueltig(
    monkeypatch, isolated_store, notes, payload_dir, quelle, mealie
):
    """Kennt der Anbieter keinen der eingestellten Namen, ist das ein
    Konfigurationsfehler: endgültig gescheitert, nicht "später nochmal" - sonst würde
    der Mensch endlos gebeten, den Link erneut zu schicken."""
    unbekannt = {"error": {"code": 404, "message": "This model is no longer available to new users."}}
    _anbieter(monkeypatch, {}, default=FakeResponse(404, unbekannt))

    asyncio.run(app_module._process_import(URL))

    zeile = isolated_store.find(url_hash(URL))
    assert zeile["status"] == "failed"
    assert mealie == []
    assert notes[0][0] == "Import fehlgeschlagen"
    assert "Rezepterkennung ist gescheitert" in notes[0][1]
