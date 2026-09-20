"""Modellkette in `llm._post`: wann wird gewechselt, wann nicht (Aufgaben 4.1 bis 4.6).

Ohne Netzzugriff und ohne Warten (DESIGN.md §12): `requests.post` wird durch eine
Zuordnung Modellname -> Antwortfolge ersetzt, `time.sleep` durch nichts. Geprüft wird
je Zeile der Tabelle aus `design.md` genau ein Fall.
"""
from __future__ import annotations

import json
import logging

import pytest

import config
import llm
import model_chain
import naming

# Ein schema-konformes Rezept, damit `_post` eine 200-Antwort auch zurückgeben kann.
VALID_ANSWER = {
    "choices": [{"message": {"content": json.dumps({
        "name": "Kartoffelsuppe mit Majoran",
        "recipeIngredient": ["800 g Kartoffeln"],
        "recipeInstructions": ["Kartoffeln schälen."],
        "recipeYield": None,
        "totalTime": None,
        "description": None,
        "recipeCategory": [],
        "url": None,
    })}}]
}

# Der gemessene Wortlaut des Anbieters, damit die Erkennung an dem hängt, was wirklich
# ankommt, und nicht an einem ausgedachten Satz (design.md, Context, 2026-09-20).
UNKNOWN_BODY = {
    "error": {
        "code": 404,
        "message": "This model models/gemini-2.5-flash is no longer available to new users.",
        "status": "NOT_FOUND",
    }
}
QUOTA_BODY = {
    "error": {"code": 429, "message": "You exceeded your current quota", "status": "RESOURCE_EXHAUSTED"}
}
BUSY_BODY = {
    "error": {
        "code": 503,
        "message": "This model is currently experiencing high demand.",
        "status": "UNAVAILABLE",
    }
}


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeProvider:
    """Antwortet je Modellname. `plan` bildet Modell -> Liste von Antworten ab; ist die
    Liste leer oder der Name nicht genannt, gilt `default`."""

    def __init__(self, plan: dict[str, list[FakeResponse]], default: FakeResponse | None = None):
        self.plan = {k: list(v) for k, v in plan.items()}
        self.default = default
        self.calls: list[str] = []

    def __call__(self, *args, **kwargs):
        model = kwargs["json"]["model"]
        self.calls.append(model)
        folge = self.plan.get(model)
        if folge:
            return folge.pop(0)
        if self.default is not None:
            return self.default
        raise AssertionError(f"Unerwarteter Aufruf an Modell {model}")

    @property
    def models(self) -> list[str]:
        """Die angesprochenen Modelle in ihrer Reihenfolge, ohne Wiederholungen."""
        out: list[str] = []
        for m in self.calls:
            if not out or out[-1] != m:
                out.append(m)
        return out


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)


@pytest.fixture
def kette():
    """Die konfigurierte Kette, frisch - `conftest.frische_modellkette` setzt sie
    ohnehin je Test zurück, dies ist nur der bequeme Zugriff auf die Namen."""
    return list(config.LLM_MODEL_CHAIN)


def _post(**kwargs):
    return llm._post([{"role": "user", "content": "x"}], **kwargs)


# --------------------------------------------------------------------------
# 4.2 - eine Zeile der Tabelle je Test
# --------------------------------------------------------------------------

def test_erstes_modell_antwortet_kein_weiteres_wird_gefragt(monkeypatch, kette):
    provider = FakeProvider({kette[0]: [FakeResponse(200, VALID_ANSWER)]})
    monkeypatch.setattr(llm.requests, "post", provider)

    assert json.loads(_post())["name"] == "Kartoffelsuppe mit Majoran"
    assert provider.models == [kette[0]]


def test_429_wird_erst_wiederholt_dann_gewechselt(monkeypatch, kette):
    """Erstes 429: zweiter Versuch auf demselben Modell. Zweites 429: nächstes Modell."""
    provider = FakeProvider(
        {
            kette[0]: [FakeResponse(429, QUOTA_BODY), FakeResponse(429, QUOTA_BODY)],
            kette[1]: [FakeResponse(200, VALID_ANSWER)],
        }
    )
    monkeypatch.setattr(llm.requests, "post", provider)

    assert json.loads(_post())["name"] == "Kartoffelsuppe mit Majoran"
    assert provider.calls == [kette[0], kette[0], kette[1]]


def test_429_sperrt_das_modell_fuer_die_naechste_anfrage(monkeypatch, kette):
    """Das ist der eigentliche Zweck der Sperrfrist: ein leeres Kontingent kostet
    einen abgelehnten Aufruf, nicht einen je Import."""
    provider = FakeProvider(
        {
            kette[0]: [FakeResponse(429, QUOTA_BODY), FakeResponse(429, QUOTA_BODY)],
            kette[1]: [FakeResponse(200, VALID_ANSWER), FakeResponse(200, VALID_ANSWER)],
        }
    )
    monkeypatch.setattr(llm.requests, "post", provider)

    _post()
    provider.calls.clear()
    _post()

    assert provider.calls == [kette[1]]


def test_503_wechselt_das_modell_ohne_sperrfrist(monkeypatch, kette):
    """Gemessen am 2026-09-20: 503 nennt die Last *eines* Modells. Also weiter - aber
    beim nächsten Aufruf steht dasselbe Modell wieder vorn."""
    provider = FakeProvider(
        {
            kette[0]: [FakeResponse(503, BUSY_BODY), FakeResponse(503, BUSY_BODY),
                       FakeResponse(200, VALID_ANSWER)],
            kette[1]: [FakeResponse(200, VALID_ANSWER)],
        }
    )
    monkeypatch.setattr(llm.requests, "post", provider)

    _post()
    assert provider.calls == [kette[0], kette[0], kette[1]]

    provider.calls.clear()
    _post()
    assert provider.calls == [kette[0]]


@pytest.mark.parametrize("status", [502, 503, 504])
def test_jede_lastmeldung_wechselt(monkeypatch, kette, status):
    provider = FakeProvider(
        {
            kette[0]: [FakeResponse(status), FakeResponse(status)],
            kette[1]: [FakeResponse(200, VALID_ANSWER)],
        }
    )
    monkeypatch.setattr(llm.requests, "post", provider)

    _post()
    assert provider.models == [kette[0], kette[1]]


def test_500_wechselt_nicht(monkeypatch, kette):
    """500 nennt keine modellabhängige Ursache - Verhalten wie vor dieser Änderung."""
    provider = FakeProvider({kette[0]: [FakeResponse(500), FakeResponse(500)]},
                            default=FakeResponse(200, VALID_ANSWER))
    monkeypatch.setattr(llm.requests, "post", provider)

    with pytest.raises(llm.LlmOverloadedError):
        _post()

    assert provider.calls == [kette[0], kette[0]]


def test_404_faellt_sofort_aus_der_kette(monkeypatch, kette):
    """Kein zweiter Versuch: ein Name, den es nicht gibt, entsteht nicht durch
    Wiederholen. Und beim nächsten Aufruf wird er gar nicht mehr angesprochen."""
    provider = FakeProvider(
        {kette[0]: [FakeResponse(404, UNKNOWN_BODY)]},
        default=FakeResponse(200, VALID_ANSWER),
    )
    monkeypatch.setattr(llm.requests, "post", provider)

    _post()
    assert provider.calls == [kette[0], kette[1]]

    provider.calls.clear()
    _post()
    assert provider.calls == [kette[1]]


def test_400_das_den_modellnamen_bemaengelt_faellt_aus_der_kette(monkeypatch, kette):
    body = {"error": {"code": 400, "message": "models/foo is not found for API version v1beta"}}
    provider = FakeProvider({kette[0]: [FakeResponse(400, body)]},
                            default=FakeResponse(200, VALID_ANSWER))
    monkeypatch.setattr(llm.requests, "post", provider)

    _post()
    assert provider.models == [kette[0], kette[1]]


def test_anderes_400_scheitert_sofort_ohne_modellwechsel(monkeypatch, kette):
    """Ein zu grosser Prompt ist auf dem nächsten Modell genauso zu gross."""
    body = {"error": {"code": 400, "message": "Request payload size exceeds the limit"}}
    provider = FakeProvider({kette[0]: [FakeResponse(400, body)]},
                            default=FakeResponse(200, VALID_ANSWER))
    monkeypatch.setattr(llm.requests, "post", provider)

    with pytest.raises(llm.LlmError) as info:
        _post()

    assert not isinstance(info.value, llm.LlmOverloadedError)
    assert provider.calls == [kette[0]]


def test_verbindungsfehler_wechselt_nicht(monkeypatch, kette):
    """Ohne Leitung hilft kein anderes Modell - es liegt hinter derselben."""
    def kaputt(*a, **k):
        raise llm.requests.RequestException("kein Netz")

    monkeypatch.setattr(llm.requests, "post", kaputt)

    with pytest.raises(llm.LlmError) as info:
        _post()
    assert "nicht erreichbar" in str(info.value)


# --------------------------------------------------------------------------
# 4.3 - was am Ende der Kette herauskommt
# --------------------------------------------------------------------------

def test_alle_modelle_429_ergibt_overloaded_und_nennt_die_kette(monkeypatch, kette):
    provider = FakeProvider({}, default=FakeResponse(429, QUOTA_BODY))
    monkeypatch.setattr(llm.requests, "post", provider)

    with pytest.raises(llm.LlmOverloadedError) as info:
        _post()

    for model in kette:
        assert model in str(info.value)
    assert provider.models == kette


def test_alle_modelle_unbekannt_ergibt_llm_error(monkeypatch, kette):
    """Kein "später nochmal": das ist ein Konfigurationsfehler, und der Nutzer soll
    nicht aufgefordert werden, den Link noch einmal zu schicken."""
    provider = FakeProvider({}, default=FakeResponse(404, UNKNOWN_BODY))
    monkeypatch.setattr(llm.requests, "post", provider)

    with pytest.raises(llm.LlmError) as info:
        _post()

    assert not isinstance(info.value, llm.LlmOverloadedError)
    for model in kette:
        assert model in str(info.value)


def test_leere_kette_vor_dem_ersten_aufruf_ist_overloaded(monkeypatch, kette):
    """Alles gesperrt: kein HTTP-Aufruf mehr, aber derselbe Wortlaut wie bisher."""
    for model in kette:
        model_chain.mark_exhausted(model)

    def darf_nicht(*a, **k):
        raise AssertionError("Es darf kein Aufruf mehr hinausgehen")

    monkeypatch.setattr(llm.requests, "post", darf_nicht)

    with pytest.raises(llm.LlmOverloadedError):
        _post()


def test_leere_kette_aus_lauter_unbekannten_ist_llm_error(monkeypatch, kette):
    for model in kette:
        model_chain.mark_unknown(model)

    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: FakeResponse(200, VALID_ANSWER))

    with pytest.raises(llm.LlmError) as info:
        _post()
    assert not isinstance(info.value, llm.LlmOverloadedError)


def test_ausgelastete_kette_bekommt_den_wortlaut_aus_paragraph_7(monkeypatch, kette):
    import app as app_module

    provider = FakeProvider({}, default=FakeResponse(429, QUOTA_BODY))
    monkeypatch.setattr(llm.requests, "post", provider)

    with pytest.raises(llm.LlmOverloadedError) as info:
        _post()

    assert app_module._describe_source_error(info.value) == (
        "Das Sprachmodell ist gerade überlastet. Bitte später erneut teilen."
    )


# --------------------------------------------------------------------------
# 4.4 - das Schema-Budget bleibt unangetastet
# --------------------------------------------------------------------------

def test_schemawiederholung_bleibt_genau_eine_ueber_den_modellwechsel_hinweg(monkeypatch, kette):
    """Erst eine schemawidrige Antwort, dann läuft die eine Wiederholung in ein 429 und
    wechselt das Modell. Insgesamt bleibt es bei genau einer Schemawiederholung, nicht
    einer je Modell."""
    kaputt = {"choices": [{"message": {"content": "kein JSON"}}]}
    provider = FakeProvider(
        {
            kette[0]: [
                FakeResponse(200, kaputt),                # erster Aufruf: falsch geformt
                FakeResponse(429, QUOTA_BODY),            # die Wiederholung, 429
                FakeResponse(429, QUOTA_BODY),
            ],
            kette[1]: [FakeResponse(200, VALID_ANSWER)],  # sie beantwortet das nächste Modell
        }
    )
    monkeypatch.setattr(llm.requests, "post", provider)

    recipe = llm.extract_recipe("Ein Rezepttext", "https://example.org/r")

    assert recipe.name == "Kartoffelsuppe mit Majoran"
    assert provider.calls == [kette[0], kette[0], kette[0], kette[1]]


def test_zweite_schemawidrige_antwort_scheitert_trotz_modellwechsel(monkeypatch, kette):
    kaputt = {"choices": [{"message": {"content": "kein JSON"}}]}
    provider = FakeProvider({}, default=FakeResponse(200, kaputt))
    monkeypatch.setattr(llm.requests, "post", provider)

    with pytest.raises(llm.LlmError) as info:
        llm.extract_recipe("Ein Rezepttext", "https://example.org/r")

    assert "zweiten Versuch" in str(info.value)
    assert len(provider.calls) == 2


# --------------------------------------------------------------------------
# 4.5 - jeder Wechsel steht im Protokoll
# --------------------------------------------------------------------------

def test_modellwechsel_wird_protokolliert(monkeypatch, caplog, kette):
    provider = FakeProvider(
        {
            kette[0]: [FakeResponse(429, QUOTA_BODY), FakeResponse(429, QUOTA_BODY)],
            kette[1]: [FakeResponse(200, VALID_ANSWER)],
        }
    )
    monkeypatch.setattr(llm.requests, "post", provider)

    with caplog.at_level(logging.WARNING):
        _post()

    wechsel = [r for r in caplog.records if "Modellwechsel" in r.getMessage()]
    assert len(wechsel) == 1
    text = wechsel[0].getMessage()
    assert kette[0] in text and kette[1] in text and "429" in text
    assert wechsel[0].levelno == logging.WARNING


def test_protokoll_traegt_keinen_schluessel(monkeypatch, caplog, kette):
    """Der Antwortkörper wird gekürzt und ohne Kopfzeilen protokolliert - ein
    Bearer-Token darf in keiner Zeile stehen."""
    provider = FakeProvider({}, default=FakeResponse(429, QUOTA_BODY))
    monkeypatch.setattr(llm.requests, "post", provider)

    with caplog.at_level(logging.DEBUG), pytest.raises(llm.LlmOverloadedError):
        _post()

    for record in caplog.records:
        assert config.LLM_API_KEY not in record.getMessage()
        assert "Authorization" not in record.getMessage()


# --------------------------------------------------------------------------
# 4.6 - die Namensstufe erbt die Kette und wirft weiterhin nie
# --------------------------------------------------------------------------

def test_namensstufe_nutzt_die_kette(monkeypatch, kette):
    antwort = {"choices": [{"message": {"content": json.dumps({"name": "Kartoffelsuppe"})}}]}
    provider = FakeProvider(
        {
            kette[0]: [FakeResponse(429, QUOTA_BODY), FakeResponse(429, QUOTA_BODY)],
            kette[1]: [FakeResponse(200, antwort)],
        }
    )
    monkeypatch.setattr(llm.requests, "post", provider)
    monkeypatch.setattr(config, "NAMING_ENABLED", True)

    name = naming.make_name("Suppe von Bärchenknutscher", "https://example.org/r", ["Kartoffeln"], ["Kochen"])

    assert name == "Kartoffelsuppe"
    assert provider.models == [kette[0], kette[1]]


def test_namensstufe_wirft_auch_bei_leerer_kette_nicht(monkeypatch, caplog, kette):
    provider = FakeProvider({}, default=FakeResponse(429, QUOTA_BODY))
    monkeypatch.setattr(llm.requests, "post", provider)
    monkeypatch.setattr(config, "NAMING_ENABLED", True)

    with caplog.at_level(logging.WARNING):
        name = naming.make_name("Alter Name", "https://example.org/r", ["Kartoffeln"], ["Kochen"])

    assert name is None
    assert any("Namensstufe" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# Die Probe hängt die Kette aus (4.1, gebraucht von Abschnitt 5)
# --------------------------------------------------------------------------

def test_angepinntes_modell_umgeht_die_kette(monkeypatch, kette):
    provider = FakeProvider({"gemini-9.9-flash": [FakeResponse(200, VALID_ANSWER)]})
    monkeypatch.setattr(llm.requests, "post", provider)

    _post(model="gemini-9.9-flash")

    assert provider.calls == ["gemini-9.9-flash"]


def test_angepinntes_modell_sperrt_nichts_in_der_kette(monkeypatch, kette):
    """Ein Modell, das noch gar nicht in der Kette steht, darf ihre Sperrfristen nicht
    anfassen - auch dann nicht, wenn es selbst 429 antwortet."""
    provider = FakeProvider({}, default=FakeResponse(429, QUOTA_BODY))
    monkeypatch.setattr(llm.requests, "post", provider)

    with pytest.raises(llm.LlmOverloadedError):
        _post(model="gemini-9.9-flash")

    assert model_chain.candidates() == kette
    assert provider.calls == ["gemini-9.9-flash", "gemini-9.9-flash"]
