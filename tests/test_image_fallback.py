"""Der Gang durch die Bildkette (A22, Aufgaben 5.1 bis 5.6), ohne Netzzugriff.

Aufbau wie `test_llm_fallback.py`: die Kette wird gestellt, die Antworten je Kandidat
vorgegeben, und geprüft wird, **wer** angesprochen wurde und **was** sich die Kette
gemerkt hat. Die Uhr der Stufe (`image._now`) wird von Hand geschoben, geschlafen wird
nie (DESIGN.md §12).
"""
from __future__ import annotations

import base64
import logging
from unittest.mock import MagicMock

import pytest

import config
import image
import image_chain
from conftest import FIXTURES_DIR

JPEG_BYTES = (FIXTURES_DIR / "document_rezeptfoto.jpg").read_bytes()
JPEG_B64 = base64.b64encode(JPEG_BYTES).decode("ascii")

PRO = ("gemini", "gemini-3-pro-image")
FLASH = ("gemini", "gemini-3.1-flash-image")
FREI = ("pollinations", "sana")


class _Response:
    def __init__(self, status_code=200, payload=None, content=b"", text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = text
        self.headers = headers if headers is not None else {"Content-Type": "image/jpeg"}

    def json(self):
        if self._payload is None:
            raise ValueError("keine JSON-Antwort")
        return self._payload


def _bild_antwort() -> _Response:
    return _Response(
        payload={
            "candidates": [
                {"content": {"parts": [{"inlineData": {"mimeType": "image/jpeg", "data": JPEG_B64}}]}}
            ]
        }
    )


@pytest.fixture
def kette(monkeypatch):
    """Die Vorgabekette: zwei bezahlte Gemini-Kandidaten, darunter der kostenlose."""
    monkeypatch.setattr(config, "IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "IMAGE_MODEL_CHAIN", [PRO, FLASH, FREI])
    image_chain.reset()


def _posts(monkeypatch, *antworten):
    """Antworten der beiden Gemini-Kandidaten, in dieser Reihenfolge."""
    mock = MagicMock(side_effect=list(antworten))
    monkeypatch.setattr(image.requests, "post", mock)
    return mock


def _gets(monkeypatch, *antworten):
    """Antworten des Pollinations-Kandidaten."""
    mock = MagicMock(side_effect=list(antworten))
    monkeypatch.setattr(image.requests, "get", mock)
    return mock


def _modelle(post_mock) -> list[str]:
    """Die Modellnamen, die wirklich angesprochen wurden - aus der URL gelesen."""
    return [aufruf.args[0].rsplit("/models/", 1)[1].split(":")[0] for aufruf in post_mock.call_args_list]


# --- Aufgabe 5.1: der Gang selbst -----------------------------------------------------


def test_zweiter_kandidat_liefert_das_bild(monkeypatch, kette):
    post = _posts(monkeypatch, _Response(status_code=503, text="overloaded"), _bild_antwort())
    get = _gets(monkeypatch)

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) == JPEG_BYTES
    assert _modelle(post) == [PRO[1], FLASH[1]]
    get.assert_not_called()


def test_erster_kandidat_liefert_und_niemand_sonst_wird_gefragt(monkeypatch, kette):
    post = _posts(monkeypatch, _bild_antwort())
    get = _gets(monkeypatch)

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) == JPEG_BYTES
    assert _modelle(post) == [PRO[1]]
    get.assert_not_called()


def test_der_kostenlose_boden_faengt_die_bezahlten_auf(monkeypatch, kette):
    """Guthaben leer: beide Gemini-Kandidaten antworten 429, das Bild kommt vom freien."""
    post = _posts(
        monkeypatch,
        _Response(status_code=429, text="RESOURCE_EXHAUSTED"),
        _Response(status_code=429, text="RESOURCE_EXHAUSTED"),
    )
    get = _gets(monkeypatch, _Response(content=JPEG_BYTES))

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) == JPEG_BYTES
    assert _modelle(post) == [PRO[1], FLASH[1]]
    assert get.call_count == 1


def test_ohne_brauchbaren_kandidaten_bleibt_das_rezept_ohne_bild(monkeypatch, kette):
    _posts(monkeypatch, _Response(status_code=500, text="kaputt"), _Response(status_code=500, text="kaputt"))
    _gets(monkeypatch, _Response(status_code=500, text="kaputt"))

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None


# --- Aufgabe 5.2: die drei Eimer der Einordnung ---------------------------------------


def test_429_sperrt_den_kandidaten_fuer_die_frist(monkeypatch, kette):
    _posts(monkeypatch, _Response(status_code=429, text="quota exceeded"), _bild_antwort())
    _gets(monkeypatch)

    image.generate("Kartoffelsuppe", ["800 g Kartoffeln"])

    assert image_chain.candidates() == [FLASH, FREI]


@pytest.mark.parametrize("status", [402, 403], ids=["bezahlung", "verboten"])
def test_bezahlstufen_mit_guthabenhinweis_sperren_ebenfalls(monkeypatch, kette, status):
    _posts(monkeypatch, _Response(status_code=status, text="billing not enabled"), _bild_antwort())
    _gets(monkeypatch)

    image.generate("Kartoffelsuppe", ["800 g Kartoffeln"])

    assert image_chain.candidates() == [FLASH, FREI]


def test_403_ohne_guthabenhinweis_sperrt_nicht(monkeypatch, kette):
    """Ein verbotener Zugriff ist kein leeres Guthaben - der Kandidat bleibt in der Kette,
    dieser Import geht nur ohne ihn weiter."""
    _posts(monkeypatch, _Response(status_code=403, text="forbidden"), _bild_antwort())
    _gets(monkeypatch)

    image.generate("Kartoffelsuppe", ["800 g Kartoffeln"])

    assert image_chain.candidates() == [PRO, FLASH, FREI]


def test_404_nimmt_den_kandidaten_dauerhaft_aus_der_kette(monkeypatch, kette):
    _posts(monkeypatch, _Response(status_code=404, text="not found"), _bild_antwort())
    _gets(monkeypatch)

    image.generate("Kartoffelsuppe", ["800 g Kartoffeln"])

    assert image_chain.candidates() == [FLASH, FREI]
    # Kein Zeitwert: auch später nicht wieder da.
    monkeypatch.setattr(image_chain, "_now", lambda: 10**9)
    assert image_chain.candidates() == [FLASH, FREI]


def test_400_mit_unbekanntem_modellnamen_zaehlt_als_unbekannt(monkeypatch, kette):
    """Dieselben Marker wie in der Textstufe (`llm._UNKNOWN_MODEL_MARKERS`)."""
    _posts(
        monkeypatch,
        _Response(status_code=400, text="models/gemini-3-pro-image is not found for API version"),
        _bild_antwort(),
    )
    _gets(monkeypatch)

    image.generate("Kartoffelsuppe", ["800 g Kartoffeln"])

    assert image_chain.candidates() == [FLASH, FREI]


def test_unbrauchbare_antwort_merkt_sich_die_kette_nicht(monkeypatch, kette):
    """Ein 500 sagt nichts über Guthaben oder Modellnamen - beim nächsten Import ist der
    Kandidat wieder der erste."""
    _posts(monkeypatch, _Response(status_code=500, text="kaputt"), _bild_antwort())
    _gets(monkeypatch)

    image.generate("Kartoffelsuppe", ["800 g Kartoffeln"])

    assert image_chain.candidates() == [PRO, FLASH, FREI]


def test_nicht_erreichbarer_anbieter_geht_zum_naechsten(monkeypatch, kette):
    post = MagicMock(side_effect=[image.requests.RequestException("timeout"), _bild_antwort()])
    monkeypatch.setattr(image.requests, "post", post)
    _gets(monkeypatch)

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) == JPEG_BYTES
    assert image_chain.candidates() == [PRO, FLASH, FREI]


# --- Aufgabe 5.3: die Prüfungen der Bilddaten gelten je Kandidat ----------------------


def test_zu_grosses_bild_faellt_auf_den_naechsten_kandidaten(monkeypatch, kette):
    riesig = base64.b64encode(b"\xff\xd8\xff" + b"x" * (image.MAX_IMAGE_BYTES + 1)).decode("ascii")
    payload = {"candidates": [{"content": {"parts": [{"inlineData": {"data": riesig}}]}}]}
    post = _posts(monkeypatch, _Response(payload=payload), _bild_antwort())
    _gets(monkeypatch)

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) == JPEG_BYTES
    assert len(post.call_args_list) == 2


def test_bytes_ohne_bildkennung_fallen_auf_den_naechsten_kandidaten(monkeypatch, kette):
    kein_bild = base64.b64encode(b"kein Bild").decode("ascii")
    payload = {"candidates": [{"content": {"parts": [{"inlineData": {"data": kein_bild}}]}}]}
    _posts(monkeypatch, _Response(payload=payload), _bild_antwort())
    _gets(monkeypatch)

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) == JPEG_BYTES


# --- Aufgabe 5.4: das Zeitbudget der Stufe -------------------------------------------


class _Uhr:
    def __init__(self):
        self.jetzt = 500.0

    def __call__(self) -> float:
        return self.jetzt


def test_aufgebrauchtes_zeitbudget_beendet_den_gang(monkeypatch, kette):
    """Die beiden ersten Kandidaten haben so lange gebraucht, dass für den dritten nichts
    mehr übrig ist - er wird gar nicht erst angesprochen."""
    uhr = _Uhr()
    monkeypatch.setattr(image, "_now", uhr)

    def langsam(*args, **kwargs):
        uhr.jetzt += 60
        return _Response(status_code=500, text="kaputt")

    post = MagicMock(side_effect=langsam)
    monkeypatch.setattr(image.requests, "post", post)
    get = _gets(monkeypatch)

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None
    assert len(post.call_args_list) == 2
    get.assert_not_called()


def test_erreichtes_budget_stoppt_schon_vor_dem_ersten_aufruf(monkeypatch, kette):
    monkeypatch.setattr(config, "IMAGE_DEADLINE_SECONDS", 1)
    post = _posts(monkeypatch)
    get = _gets(monkeypatch)

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None
    post.assert_not_called()
    get.assert_not_called()


def test_bild_vor_dem_budgetende_kommt_ganz_normal_an(monkeypatch, kette):
    monkeypatch.setattr(image, "_now", _Uhr())
    _posts(monkeypatch, _bild_antwort())
    _gets(monkeypatch)

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) == JPEG_BYTES


# --- Aufgabe 5.5: höchstens ein Aufruf je Kandidat ------------------------------------


def test_je_kandidat_genau_ein_aufruf(monkeypatch, kette):
    post = _posts(
        monkeypatch,
        _Response(status_code=503, text="overloaded"),
        _Response(status_code=503, text="overloaded"),
    )
    get = _gets(monkeypatch, _Response(status_code=503, text="overloaded"))

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None
    assert len(post.call_args_list) == 2
    assert get.call_count == 1


def test_gesperrte_kandidaten_kosten_im_naechsten_import_keinen_aufruf(monkeypatch, kette):
    """Der eigentliche Zweck der Sperrfrist: ein leeres Guthaben kostet einen abgewiesenen
    Aufruf je Frist, nicht einen je Import."""
    _posts(
        monkeypatch,
        _Response(status_code=429, text="quota"),
        _Response(status_code=429, text="quota"),
    )
    _gets(monkeypatch, _Response(content=JPEG_BYTES), _Response(content=JPEG_BYTES))

    image.generate("Kartoffelsuppe", ["800 g Kartoffeln"])

    post = _posts(monkeypatch)
    assert image.generate("Zwiebelkuchen", ["500 g Zwiebeln"]) == JPEG_BYTES
    post.assert_not_called()


def test_gesperrte_kette_meldet_und_ruft_niemanden(monkeypatch, kette, caplog):
    for kandidat in (PRO, FLASH, FREI):
        image_chain.mark_exhausted(kandidat)
    post = _posts(monkeypatch)
    get = _gets(monkeypatch)

    with caplog.at_level(logging.WARNING):
        assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None

    post.assert_not_called()
    get.assert_not_called()
    assert "gesperrt" in caplog.text


# --- Aufgabe 5.6: was im Protokoll steht ----------------------------------------------


def test_protokoll_nennt_den_kandidaten_und_keinen_schluessel(monkeypatch, kette, caplog):
    monkeypatch.setitem(
        config.IMAGE_ENDPOINTS, "gemini", ("https://gemini.example", "test-platzhalter-llm-key")
    )
    _posts(monkeypatch, _Response(status_code=429, text="quota"), _bild_antwort())
    _gets(monkeypatch)

    with caplog.at_level(logging.INFO):
        image.generate("Kartoffelsuppe", ["800 g Kartoffeln"])

    assert PRO[1] in caplog.text  # der übersprungene Kandidat
    assert FLASH[1] in caplog.text  # der liefernde Kandidat
    assert "test-platzhalter-llm-key" not in caplog.text
