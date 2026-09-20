"""Bildstufe (A19, openspec/changes/add-ai-recipe-image), ohne Netzzugriff.

Geprüft wird auf drei Ebenen, weil die Stufe an drei Stellen danebengehen kann:
die beiden neuen Mealie-Aufrufe, die Erzeugung selbst, und die Einbettung in den
Ablauf - dort gilt die eigentliche Zusicherung, dass kein Fehlschlag dieser Stufe
je einen Import beschädigt.

Die Bilddaten sind das echte JPEG aus `tests/fixtures`, damit die Prüfung der Magic
Bytes (`llm._image_mime`) und die daraus abgeleitete Dateiendung wirklich durchlaufen.
Echte Bildkosten fallen erst beim Lauf gegen die eigene Anlage an (Aufgaben 8.1/8.2
der Änderung).
"""
from __future__ import annotations

import asyncio
import base64
from unittest.mock import MagicMock

import pytest

import app as app_module
import config
import image
import image_chain
import mealie_client
import prompts
from classify import normalize_url, url_hash
from conftest import FIXTURES_DIR
from schema import Recipe

JPEG_BYTES = (FIXTURES_DIR / "document_rezeptfoto.jpg").read_bytes()
JPEG_B64 = base64.b64encode(JPEG_BYTES).decode("ascii")

# Form der Mealie-Antwort wie in test_placeholder.py, hier um id und Bildfeld ergänzt.
# Das Feld `image` trägt in beiden eine Kennung - genau so liefert Mealie es auch für ein
# Rezept ohne Bild (Probe 1.2 vom 2026-09-20). Unterschieden wird über die Mediendatei,
# siehe `_media_answers`.
SCRAPED_RECIPE_WITH_IMAGE = {
    "id": "11111111-1111-1111-1111-111111111111",
    "name": "Apfelkuchen vom Blech",
    "image": "qTbn",
    "recipeIngredient": [{"note": "360 g Mehl", "display": "360 g Mehl"}],
    "recipeInstructions": [{"text": "Teig kneten."}],
}

SCRAPED_RECIPE_WITHOUT_IMAGE = {
    "id": "22222222-2222-2222-2222-222222222222",
    "name": "Apfelkuchen vom Blech",
    "image": "qTbn",
    "recipeIngredient": [{"note": "360 g Mehl", "display": "360 g Mehl"}],
    "recipeInstructions": [{"text": "Teig kneten."}],
}


class _Response:
    """Minimale Antwort, wie `requests` sie liefert - nur die vier Felder, die die
    Bildstufe liest."""

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


def _media_answers(monkeypatch):
    """Antwortet auf die Bildstandsabfrage (`has_image`) nach der id im Pfad: das
    Rezept mit Bild bekommt 200, das ohne 404. So laufen die Ablauftests durch dieselbe
    Weiche wie der Dienst, ohne Netz."""

    def fake_get(url, headers=None, timeout=None):
        if SCRAPED_RECIPE_WITH_IMAGE["id"] in url:
            return _Response(status_code=200, content=JPEG_BYTES)
        return _Response(status_code=404)

    monkeypatch.setattr(mealie_client.requests, "get", fake_get)


def _kette(monkeypatch, *kandidaten):
    """Stellt die Bildkette für einen Test. Ohne das liefe jeder Anbietertest die
    Vorgabekette von oben durch und spräche dabei Anbieter an, die er gar nicht prüft."""
    monkeypatch.setattr(config, "IMAGE_MODEL_CHAIN", list(kandidaten))
    image_chain.reset()


@pytest.fixture
def image_on(monkeypatch):
    """Hebt die autouse-Abschaltung aus conftest.py für die Tests auf, die die Stufe
    selbst prüfen. Die Kette steht dabei auf dem einen Anbieter, den der jeweilige
    Abschnitt prüft; die Kettentests weiter unten stellen sie selbst."""
    monkeypatch.setattr(config, "IMAGE_ENABLED", True)
    _kette(monkeypatch, ("pollinations", "sana"))


def _recipe() -> Recipe:
    return Recipe(
        name="Kartoffelsuppe mit Majoran",
        recipeIngredient=["800 g Kartoffeln", "1 l Gemüsebrühe"],
        recipeInstructions=["Kartoffeln schälen.", "In der Brühe garen."],
    )


# --- mealie_client.has_image (Aufgabe 2.2) ---------------------------------------


def test_has_image_true_when_media_file_exists(monkeypatch):
    get_mock = MagicMock(return_value=_Response(status_code=200, content=JPEG_BYTES))
    monkeypatch.setattr(mealie_client.requests, "get", get_mock)

    assert mealie_client.has_image(SCRAPED_RECIPE_WITH_IMAGE) is True
    # Gefragt wird die Mediendatei des Rezepts, nicht das Feld `image`.
    url = get_mock.call_args.args[0]
    assert url.endswith(f"/api/media/recipes/{SCRAPED_RECIPE_WITH_IMAGE['id']}/images/original.webp")


def test_has_image_false_when_media_file_is_missing(monkeypatch):
    """Der Fall aus Probe 1.2: `image` trägt eine Kennung, hinter der nichts liegt."""
    monkeypatch.setattr(
        mealie_client.requests, "get", MagicMock(return_value=_Response(status_code=404))
    )
    assert mealie_client.has_image(SCRAPED_RECIPE_WITHOUT_IMAGE) is False


def test_has_image_true_when_recipe_has_no_id(monkeypatch):
    """Ohne id ist der Bildstand nicht feststellbar - dann lieber kein erzeugtes Bild
    als ein überschriebenes Foto."""
    get_mock = MagicMock()
    monkeypatch.setattr(mealie_client.requests, "get", get_mock)

    assert mealie_client.has_image({"name": "ohne id"}) is True
    get_mock.assert_not_called()


@pytest.mark.parametrize("status", [401, 500], ids=["abgewiesen", "serverfehler"])
def test_has_image_true_on_unexpected_status(monkeypatch, status):
    monkeypatch.setattr(
        mealie_client.requests, "get", MagicMock(return_value=_Response(status_code=status))
    )
    assert mealie_client.has_image(SCRAPED_RECIPE_WITH_IMAGE) is True


def test_has_image_true_when_mealie_unreachable(monkeypatch):
    monkeypatch.setattr(
        mealie_client.requests,
        "get",
        MagicMock(side_effect=mealie_client.requests.RequestException("kein Netz")),
    )
    assert mealie_client.has_image(SCRAPED_RECIPE_WITH_IMAGE) is True


# --- mealie_client.set_image (Aufgabe 2.1) ---------------------------------------


def test_set_image_sends_multipart_with_extension(monkeypatch):
    put_mock = MagicMock(return_value=_Response(status_code=200))
    monkeypatch.setattr(mealie_client.requests, "put", put_mock)

    mealie_client.set_image("kartoffelsuppe", JPEG_BYTES)

    url = put_mock.call_args.args[0]
    kwargs = put_mock.call_args.kwargs
    assert url.endswith("/api/recipes/kartoffelsuppe/image")
    # Multipart, deshalb ohne den JSON-Content-Type aus _HEADERS: requests muss die
    # boundary selbst setzen.
    assert "Content-Type" not in kwargs["headers"]
    assert kwargs["headers"]["Authorization"].startswith("Bearer ")
    filename, data, mime = kwargs["files"]["image"]
    assert filename == "image.jpg"
    assert data == JPEG_BYTES
    assert mime == "image/jpeg"
    assert kwargs["data"] == {"extension": "jpg"}


def test_set_image_raises_mealie_error_on_rejection(monkeypatch):
    monkeypatch.setattr(
        mealie_client.requests, "put", MagicMock(return_value=_Response(status_code=422, text="nope"))
    )
    with pytest.raises(mealie_client.MealieError):
        mealie_client.set_image("kartoffelsuppe", JPEG_BYTES)


def test_set_image_raises_mealie_error_for_unknown_format(monkeypatch):
    put_mock = MagicMock()
    monkeypatch.setattr(mealie_client.requests, "put", put_mock)
    with pytest.raises(mealie_client.MealieError):
        mealie_client.set_image("kartoffelsuppe", b"kein bild")
    put_mock.assert_not_called()


# --- Prompt (Aufgabe 4.1, Fassung aus 9.2) ---------------------------------------


def test_prompt_carries_the_recipe_and_leaves_no_placeholder():
    rendered = prompts.IMAGE_GENERATION_PROMPT_TEMPLATE.format(
        name="Kartoffelsuppe mit Majoran",
        ingredients="800 g Kartoffeln, 1 l Gemüsebrühe",
    )
    assert "Kartoffelsuppe mit Majoran" in rendered
    assert "800 g Kartoffeln" in rendered
    assert "{" not in rendered and "}" not in rendered
    # Die beiden teuren Fehler dieser Stufe stehen ausdrücklich im Prompt.
    assert "no text" in rendered and "no logo" in rendered


def test_prompt_stays_short_for_a_normal_recipe():
    """Probe 1.3 vom 2026-09-20: der lange Prompt (761 Zeichen) lieferte einen leeren
    Teller mit Pseudo-Schrift, der kompakte ein brauchbares Bild. Die Grenze hält diese
    Erkenntnis fest, auch gegen eine spätere Erweiterung des Prompts."""
    rendered = image._prompt(
        "Ofen-Süßkartoffeln mit Feta und Granatapfel",
        ["800 g Süßkartoffeln", "200 g Feta", "1 Granatapfel", "3 EL Olivenöl", "1 TL Kreuzkümmel"],
    )
    assert len(rendered) < 400


def test_prompt_bounds_a_long_ingredient_list():
    rendered = image._prompt("Eintopf", [f"Zutat {i}" for i in range(40)])
    assert "Zutat 0" in rendered
    assert "Zutat 39" not in rendered
    assert rendered.count("Zutat") == image.MAX_INGREDIENTS


# --- image.generate, Anbieter pollinations (Aufgaben 4.2 und 9.3) ------------------
#
# Vorgabeanbieter, deshalb ohne eigene Fixture: die Testreihe läuft mit derselben
# Konfiguration wie der Dienst.


@pytest.fixture
def openai_provider(monkeypatch):
    """Schaltet auf den OpenAI-kompatiblen Weg, samt Schlüssel - der ist bei der
    Vorgabe leer. Adresse und Schlüssel stehen seit A22 je Anbieter in
    `config.IMAGE_ENDPOINTS`, deshalb wird dort gesetzt."""
    monkeypatch.setattr(config, "IMAGE_PROVIDER", "openai")
    monkeypatch.setitem(
        config.IMAGE_ENDPOINTS, "openai", ("https://llm.example/v1", "test-platzhalter-image-key")
    )
    _kette(monkeypatch, ("openai", config.IMAGE_MODEL))


def test_pollinations_returns_image_bytes(monkeypatch, image_on):
    get_mock = MagicMock(return_value=_Response(content=JPEG_BYTES))
    monkeypatch.setattr(image.requests, "get", get_mock)

    data = image.generate("Kartoffelsuppe mit Majoran", ["800 g Kartoffeln"])

    assert data == JPEG_BYTES
    url = get_mock.call_args.args[0]
    assert url.startswith("https://image.pollinations.ai/prompt/")
    # Der Prompt steht urlkodiert im Pfad, nicht in einem JSON-Körper.
    assert "Kartoffelsuppe%20mit%20Majoran" in url
    assert "800%20g%20Kartoffeln" in url
    assert get_mock.call_args.kwargs["params"]["model"] == config.IMAGE_MODEL
    assert get_mock.call_args.kwargs["timeout"] == image.TIMEOUT_SECONDS
    assert "json" not in get_mock.call_args.kwargs


def test_pollinations_sends_no_authorization_without_a_key(monkeypatch, image_on):
    """Der Vorgabeanbieter braucht keinen Schlüssel, und der des Textmodells darf ihn
    nie zu sehen bekommen."""
    get_mock = MagicMock(return_value=_Response(content=JPEG_BYTES))
    monkeypatch.setattr(image.requests, "get", get_mock)

    image.generate("Kartoffelsuppe", ["800 g Kartoffeln"])

    assert get_mock.call_args.kwargs["headers"] == {}


def test_pollinations_sends_a_token_when_one_is_configured(monkeypatch, image_on):
    monkeypatch.setitem(
        config.IMAGE_ENDPOINTS,
        "pollinations",
        ("https://image.pollinations.ai", "test-platzhalter-pollinations-token"),
    )
    get_mock = MagicMock(return_value=_Response(content=JPEG_BYTES))
    monkeypatch.setattr(image.requests, "get", get_mock)

    image.generate("Kartoffelsuppe", ["800 g Kartoffeln"])

    assert get_mock.call_args.kwargs["headers"]["Authorization"].endswith("pollinations-token")


def test_pollinations_returns_none_on_error_status(monkeypatch, image_on):
    """429 heisst bei diesem Anbieter "noch eine Anfrage von dir läuft". Ohne zweiten
    Anlauf: das Rezept bleibt eben ohne Bild."""
    monkeypatch.setattr(
        image.requests,
        "get",
        MagicMock(return_value=_Response(status_code=429, text="rate limit", headers={})),
    )
    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None


def test_pollinations_returns_none_when_answer_is_not_an_image(monkeypatch, image_on):
    """Ein Fehlertext mit Status 200 ist kein Bild - der Typ der Antwort entscheidet."""
    monkeypatch.setattr(
        image.requests,
        "get",
        MagicMock(
            return_value=_Response(
                content=b"<html>Fehler</html>", text="<html>Fehler</html>",
                headers={"Content-Type": "text/html"},
            )
        ),
    )
    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None


def test_pollinations_returns_none_when_provider_unreachable(monkeypatch, image_on):
    monkeypatch.setattr(
        image.requests, "get", MagicMock(side_effect=image.requests.RequestException("timeout"))
    )
    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None


def test_pollinations_returns_none_for_bytes_that_are_no_image(monkeypatch, image_on):
    monkeypatch.setattr(
        image.requests, "get", MagicMock(return_value=_Response(content=b"kein Bild"))
    )
    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None


def test_generate_is_skipped_when_disabled(monkeypatch):
    """Die autouse-Abschaltung aus conftest.py gilt, `image_on` fehlt hier bewusst."""
    get_mock = MagicMock()
    post_mock = MagicMock()
    monkeypatch.setattr(image.requests, "get", get_mock)
    monkeypatch.setattr(image.requests, "post", post_mock)

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None
    get_mock.assert_not_called()
    post_mock.assert_not_called()


# --- image.generate, Anbieter gemini (A22, Aufgaben 4.1 und 4.2) ------------------
#
# Die Form von Anfrage und Antwort ist die am 2026-09-20 gemessene (design.md, Context),
# nicht die aus der Dokumentation gelesene.


@pytest.fixture
def gemini_provider(monkeypatch):
    """Schaltet auf die native Gemini-Fläche, mit eigener Adresse und eigenem Schlüssel."""
    monkeypatch.setattr(config, "IMAGE_PROVIDER", "gemini")
    monkeypatch.setattr(config, "IMAGE_MODEL", "gemini-3-pro-image")
    # Genau der Wert, den `config` aus LLM_BASE_URL ableitet: die Wurzel der nativen
    # Fläche samt Versionsteil. Ein ausgedachter Wert hätte den doppelten `/v1beta`-Pfad
    # nicht auffallen lassen, den erst der Lauf gegen die Anlage zeigte (404).
    monkeypatch.setitem(
        config.IMAGE_ENDPOINTS,
        "gemini",
        (config.LLM_BASE_URL.removesuffix("/openai"), "test-platzhalter-llm-key"),
    )
    _kette(monkeypatch, ("gemini", "gemini-3-pro-image"))


def _gemini_answer(*parts) -> dict:
    return {"candidates": [{"content": {"parts": list(parts)}}]}


def _bildteil(daten: str = JPEG_B64, mime: str = "image/jpeg") -> dict:
    return {"inlineData": {"mimeType": mime, "data": daten}}


def test_gemini_sends_the_measured_request_shape(monkeypatch, image_on, gemini_provider):
    post_mock = MagicMock(return_value=_Response(payload=_gemini_answer(_bildteil())))
    monkeypatch.setattr(image.requests, "post", post_mock)

    data = image.generate("Kartoffelsuppe mit Majoran", ["800 g Kartoffeln"])

    assert data == JPEG_BYTES
    url = post_mock.call_args.args[0]
    kwargs = post_mock.call_args.kwargs
    assert url == (
        "https://generativelanguage.googleapis.com/v1beta"
        "/models/gemini-3-pro-image:generateContent"
    )
    # Der Versionsteil steht genau einmal drin.
    assert url.count("/v1beta/") == 1
    # Der Schlüssel geht im anbietereigenen Kopf mit, nicht als Bearer-Token.
    assert kwargs["headers"]["x-goog-api-key"] == "test-platzhalter-llm-key"
    assert "Authorization" not in kwargs["headers"]

    payload = kwargs["json"]
    assert payload["contents"] == [
        {"parts": [{"text": image._prompt("Kartoffelsuppe mit Majoran", ["800 g Kartoffeln"])}]}
    ]
    assert payload["generationConfig"]["responseModalities"] == ["IMAGE"]
    # Quadratisch zum selben Preis: Mealies Kachel würde 1408x768 beschneiden.
    assert payload["generationConfig"]["imageConfig"]["aspectRatio"] == "1:1"
    assert kwargs["timeout"] == image.GEMINI_TIMEOUT_SECONDS


def test_gemini_reads_the_image_part_after_a_text_part(monkeypatch, image_on, gemini_provider):
    """Die Antwort darf Text **und** Bild tragen; gesucht ist der erste Bildteil."""
    payload = _gemini_answer({"text": "Hier ist dein Bild:"}, _bildteil())
    monkeypatch.setattr(image.requests, "post", MagicMock(return_value=_Response(payload=payload)))

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) == JPEG_BYTES


def test_gemini_returns_none_when_the_model_answers_with_words(
    monkeypatch, image_on, gemini_provider
):
    """Ein Modell, das antwortet statt zu malen, ist eine unbrauchbare Antwort - kein
    Fehler des Anbieters."""
    payload = _gemini_answer({"text": "Ich kann dieses Bild nicht erzeugen."})
    monkeypatch.setattr(image.requests, "post", MagicMock(return_value=_Response(payload=payload)))

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None


def test_gemini_returns_none_without_candidates(monkeypatch, image_on, gemini_provider):
    monkeypatch.setattr(
        image.requests, "post", MagicMock(return_value=_Response(payload={"candidates": []}))
    )

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None


def test_gemini_returns_none_for_undecodable_base64(monkeypatch, image_on, gemini_provider):
    payload = _gemini_answer(_bildteil(daten="!!! kein base64 !!!"))
    monkeypatch.setattr(image.requests, "post", MagicMock(return_value=_Response(payload=payload)))

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None


def test_gemini_returns_none_on_error_status(monkeypatch, image_on, gemini_provider):
    monkeypatch.setattr(
        image.requests,
        "post",
        MagicMock(return_value=_Response(status_code=429, text="RESOURCE_EXHAUSTED")),
    )

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None


# --- image.generate, Anbieter openai (Aufgabe 4.2) --------------------------------


def test_openai_returns_image_bytes(monkeypatch, image_on, openai_provider):
    post_mock = MagicMock(return_value=_Response(payload={"data": [{"b64_json": JPEG_B64}]}))
    monkeypatch.setattr(image.requests, "post", post_mock)

    data = image.generate("Kartoffelsuppe mit Majoran", ["800 g Kartoffeln"])

    assert data == JPEG_BYTES
    url = post_mock.call_args.args[0]
    payload = post_mock.call_args.kwargs["json"]
    assert url == "https://llm.example/v1/images/generations"
    assert payload["model"] == config.IMAGE_MODEL
    assert payload["n"] == 1
    assert "Kartoffelsuppe mit Majoran" in payload["prompt"]
    assert "800 g Kartoffeln" in payload["prompt"]


def test_openai_returns_none_on_error_status(monkeypatch, image_on, openai_provider):
    monkeypatch.setattr(
        image.requests, "post", MagicMock(return_value=_Response(status_code=429, text="slow down"))
    )
    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None


def test_openai_returns_none_when_provider_unreachable(monkeypatch, image_on, openai_provider):
    monkeypatch.setattr(
        image.requests, "post", MagicMock(side_effect=image.requests.RequestException("timeout"))
    )
    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": []},
        {"data": [{}]},
        {"data": ["kein objekt"]},
        {"data": [{"b64_json": ""}]},
    ],
    ids=["leer", "ohne-eintrag", "ohne-felder", "kein-objekt", "leerer-string"],
)
def test_openai_returns_none_on_unusable_body(monkeypatch, image_on, openai_provider, payload):
    monkeypatch.setattr(image.requests, "post", MagicMock(return_value=_Response(payload=payload)))
    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None


def test_openai_returns_none_for_bytes_that_are_no_image(monkeypatch, image_on, openai_provider):
    encoded = base64.b64encode(b"<html>kein Bild</html>").decode("ascii")
    monkeypatch.setattr(
        image.requests, "post", MagicMock(return_value=_Response(payload={"data": [{"b64_json": encoded}]}))
    )
    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None


def test_openai_downloads_url_when_provider_sends_no_bytes(monkeypatch, image_on, openai_provider):
    monkeypatch.setattr(
        image.requests,
        "post",
        MagicMock(return_value=_Response(payload={"data": [{"url": "https://cdn.example/bild.jpg"}]})),
    )
    get_mock = MagicMock(return_value=_Response(content=JPEG_BYTES))
    monkeypatch.setattr(image.requests, "get", get_mock)

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) == JPEG_BYTES
    assert get_mock.call_args.args[0] == "https://cdn.example/bild.jpg"


def test_openai_returns_none_when_url_download_fails(monkeypatch, image_on, openai_provider):
    monkeypatch.setattr(
        image.requests,
        "post",
        MagicMock(return_value=_Response(payload={"data": [{"url": "https://cdn.example/bild.jpg"}]})),
    )
    monkeypatch.setattr(image.requests, "get", MagicMock(return_value=_Response(status_code=404)))

    assert image.generate("Kartoffelsuppe", ["800 g Kartoffeln"]) is None


# --- Einbettung in den Ablauf (Aufgaben 5.1 und 5.2) ------------------------------


def _youtube_import(monkeypatch, recipe: Recipe):
    """Der YouTube-Weg als kürzester Vertreter aller Wege über `create_from_jsonld`:
    dort trägt das Rezept nie ein Bild von der Quelle."""
    monkeypatch.setattr(app_module, "classify", lambda url: "youtube")
    monkeypatch.setattr(app_module.youtube, "fetch", lambda url: MagicMock(text="Rezepttext"))
    monkeypatch.setattr(app_module, "extract_recipe", lambda text, url: recipe)
    monkeypatch.setattr(
        app_module.mealie_client, "create_from_jsonld", MagicMock(return_value="kartoffelsuppe")
    )
    monkeypatch.setattr(app_module.mealie_client, "recipe_link", lambda slug: f"http://mealie.local/g/home/r/{slug}")


def test_generated_image_is_uploaded_and_tagged(monkeypatch, isolated_store, image_on):
    _youtube_import(monkeypatch, _recipe())
    generate_mock = MagicMock(return_value=JPEG_BYTES)
    monkeypatch.setattr(app_module.image, "generate", generate_mock)
    set_image_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "set_image", set_image_mock)
    set_tags_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "set_tags", set_tags_mock)
    notify_mock = MagicMock()
    monkeypatch.setattr(app_module.ha_notify, "notify", notify_mock)

    url = "https://www.youtube.com/watch?v=abc12345678"
    asyncio.run(app_module._process_import(url))

    # Das Modell bekommt Name, Zutaten und Schritte des gerade angelegten Rezepts.
    assert generate_mock.call_args.args[0] == "Kartoffelsuppe mit Majoran"
    assert generate_mock.call_args.args[1] == ["800 g Kartoffeln", "1 l Gemüsebrühe"]
    set_image_mock.assert_called_once_with("kartoffelsuppe", JPEG_BYTES)
    # Ein einziges PATCH mit beiden Tags.
    set_tags_mock.assert_called_once_with("kartoffelsuppe", ["auto-import", app_module.IMAGE_TAG])
    assert notify_mock.call_args.args[0] == "Rezept angelegt"
    assert notify_mock.call_args.args[1] == "Kartoffelsuppe mit Majoran"
    assert isolated_store.find(url_hash(normalize_url(url)))["status"] == "done"


def test_scraped_image_is_never_replaced(monkeypatch, isolated_store, image_on):
    """Hat Mealie von der Quellseite ein Foto geholt, zeigt es das echte Gericht. Es
    wird weder ersetzt noch entsteht ein Bildaufruf."""
    monkeypatch.setattr(app_module, "classify", lambda url: "site")
    monkeypatch.setattr(app_module.mealie_client, "import_url", MagicMock(return_value="apfelkuchen"))
    monkeypatch.setattr(app_module.mealie_client, "get_recipe", lambda slug: SCRAPED_RECIPE_WITH_IMAGE)
    _media_answers(monkeypatch)
    generate_mock = MagicMock()
    monkeypatch.setattr(app_module.image, "generate", generate_mock)
    set_image_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "set_image", set_image_mock)
    set_tags_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "set_tags", set_tags_mock)
    monkeypatch.setattr(app_module.mealie_client, "recipe_link", lambda slug: f"http://mealie.local/g/home/r/{slug}")
    monkeypatch.setattr(app_module.ha_notify, "notify", MagicMock())

    asyncio.run(app_module._process_import("https://example.com/rezepte/apfelkuchen"))

    generate_mock.assert_not_called()
    set_image_mock.assert_not_called()
    set_tags_mock.assert_called_once_with("apfelkuchen", ["auto-import"])


def test_scraped_recipe_without_image_gets_one(monkeypatch, isolated_store, image_on):
    """Gegenprobe: Mealie hat geschabt, aber kein Bild gefunden. Dann speist sich die
    Bilderzeugung aus Mealies eigener Antwort."""
    monkeypatch.setattr(app_module, "classify", lambda url: "site")
    monkeypatch.setattr(app_module.mealie_client, "import_url", MagicMock(return_value="apfelkuchen"))
    monkeypatch.setattr(app_module.mealie_client, "get_recipe", lambda slug: SCRAPED_RECIPE_WITHOUT_IMAGE)
    _media_answers(monkeypatch)
    generate_mock = MagicMock(return_value=JPEG_BYTES)
    monkeypatch.setattr(app_module.image, "generate", generate_mock)
    monkeypatch.setattr(app_module.mealie_client, "set_image", MagicMock())
    set_tags_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "set_tags", set_tags_mock)
    monkeypatch.setattr(app_module.mealie_client, "recipe_link", lambda slug: f"http://mealie.local/g/home/r/{slug}")
    monkeypatch.setattr(app_module.ha_notify, "notify", MagicMock())

    asyncio.run(app_module._process_import("https://example.com/rezepte/apfelkuchen"))

    assert generate_mock.call_args.args[0] == "Apfelkuchen vom Blech"
    assert generate_mock.call_args.args[1] == ["360 g Mehl"]
    set_tags_mock.assert_called_once_with("apfelkuchen", ["auto-import", app_module.IMAGE_TAG])


def test_unreachable_mealie_skips_the_image_stage(monkeypatch, isolated_store, image_on):
    """Konnte schon die Platzhalter-Prüfung Mealie nicht erreichen, ist unbekannt, ob
    ein Bild da ist - dann kein Bildaufruf, wie bei der Namensstufe."""
    monkeypatch.setattr(app_module, "classify", lambda url: "site")
    monkeypatch.setattr(app_module.mealie_client, "import_url", MagicMock(return_value="apfelkuchen"))
    monkeypatch.setattr(
        app_module.mealie_client, "get_recipe", MagicMock(side_effect=mealie_client.MealieError("timeout"))
    )
    generate_mock = MagicMock()
    monkeypatch.setattr(app_module.image, "generate", generate_mock)
    monkeypatch.setattr(app_module.mealie_client, "get_recipe_name", MagicMock(return_value="Apfelkuchen"))
    set_tags_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "set_tags", set_tags_mock)
    monkeypatch.setattr(app_module.mealie_client, "recipe_link", lambda slug: f"http://mealie.local/g/home/r/{slug}")
    monkeypatch.setattr(app_module.ha_notify, "notify", MagicMock())

    asyncio.run(app_module._process_import("https://example.com/rezepte/apfelkuchen"))

    generate_mock.assert_not_called()
    set_tags_mock.assert_called_once_with("apfelkuchen", ["auto-import"])


def test_failed_generation_leaves_import_done_and_untagged(monkeypatch, isolated_store, image_on):
    _youtube_import(monkeypatch, _recipe())
    monkeypatch.setattr(app_module.image, "generate", MagicMock(return_value=None))
    set_image_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "set_image", set_image_mock)
    set_tags_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "set_tags", set_tags_mock)
    notify_mock = MagicMock()
    monkeypatch.setattr(app_module.ha_notify, "notify", notify_mock)

    url = "https://www.youtube.com/watch?v=abc12345678"
    asyncio.run(app_module._process_import(url))

    set_image_mock.assert_not_called()
    set_tags_mock.assert_called_once_with("kartoffelsuppe", ["auto-import"])
    # Der Import bleibt erfolgreich, die Meldung unverändert.
    assert notify_mock.call_args.args[0] == "Rezept angelegt"
    assert notify_mock.call_args.args[1] == "Kartoffelsuppe mit Majoran"
    assert notify_mock.call_args.args[2] == "http://mealie.local/g/home/r/kartoffelsuppe"
    assert isolated_store.find(url_hash(normalize_url(url)))["status"] == "done"


def test_rejected_upload_leaves_import_done_and_untagged(monkeypatch, isolated_store, image_on):
    _youtube_import(monkeypatch, _recipe())
    monkeypatch.setattr(app_module.image, "generate", MagicMock(return_value=JPEG_BYTES))
    monkeypatch.setattr(
        app_module.mealie_client, "set_image", MagicMock(side_effect=mealie_client.MealieError("422"))
    )
    set_tags_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "set_tags", set_tags_mock)
    notify_mock = MagicMock()
    monkeypatch.setattr(app_module.ha_notify, "notify", notify_mock)

    url = "https://www.youtube.com/watch?v=abc12345678"
    asyncio.run(app_module._process_import(url))

    set_tags_mock.assert_called_once_with("kartoffelsuppe", ["auto-import"])
    assert notify_mock.call_args.args[0] == "Rezept angelegt"
    assert isolated_store.find(url_hash(normalize_url(url)))["status"] == "done"


def test_repeated_import_makes_no_image_call(monkeypatch, isolated_store, image_on):
    """Aufgabe 5.2: die Dublettenmeldung (`_notify_if_done`) läuft an der Bildstufe
    vorbei - ein zweites Teilen derselben Quelle kostet kein Bild."""
    _youtube_import(monkeypatch, _recipe())
    generate_mock = MagicMock(return_value=JPEG_BYTES)
    monkeypatch.setattr(app_module.image, "generate", generate_mock)
    monkeypatch.setattr(app_module.mealie_client, "set_image", MagicMock())
    monkeypatch.setattr(app_module.mealie_client, "set_tags", MagicMock())
    notify_mock = MagicMock()
    monkeypatch.setattr(app_module.ha_notify, "notify", notify_mock)

    url = "https://www.youtube.com/watch?v=abc12345678"
    asyncio.run(app_module._process_import(url))
    asyncio.run(app_module._process_import(url))

    assert generate_mock.call_count == 1
    assert notify_mock.call_args.args[0] == "Rezept schon vorhanden"


def test_no_image_call_when_the_import_fails_before_mealie(monkeypatch, isolated_store, image_on):
    """Scheitert die Extraktion, existiert kein Rezept - und es entsteht kein
    Bildaufruf, der nichts hätte, woran er hängen könnte."""
    monkeypatch.setattr(app_module, "classify", lambda url: "youtube")
    monkeypatch.setattr(app_module.youtube, "fetch", lambda url: MagicMock(text="Rezepttext"))

    class NoTranscriptError(Exception):
        pass

    def fail(text, url):
        raise NoTranscriptError("keine Untertitel")

    monkeypatch.setattr(app_module, "extract_recipe", fail)
    generate_mock = MagicMock()
    monkeypatch.setattr(app_module.image, "generate", generate_mock)
    monkeypatch.setattr(app_module.ha_notify, "notify", MagicMock())

    asyncio.run(app_module._process_import("https://www.youtube.com/watch?v=abc12345678"))

    generate_mock.assert_not_called()


def test_disabled_stage_makes_no_call_at_all(monkeypatch, isolated_store):
    """Abgeschaltete Stufe heisst: kein Bildaufruf **und** keine Bildstandsabfrage.
    Seit `has_image` Mealies Mediendatei abfragt (Probe 1.2), ist auch das ein
    Netzaufruf - `image_on` fehlt hier bewusst, es gilt die Abschaltung aus conftest."""
    monkeypatch.setattr(app_module, "classify", lambda url: "site")
    monkeypatch.setattr(app_module.mealie_client, "import_url", MagicMock(return_value="apfelkuchen"))
    monkeypatch.setattr(app_module.mealie_client, "get_recipe", lambda slug: SCRAPED_RECIPE_WITHOUT_IMAGE)
    get_mock = MagicMock()
    monkeypatch.setattr(mealie_client.requests, "get", get_mock)
    generate_mock = MagicMock()
    monkeypatch.setattr(app_module.image, "generate", generate_mock)
    set_tags_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "set_tags", set_tags_mock)
    monkeypatch.setattr(app_module.mealie_client, "recipe_link", lambda slug: f"http://mealie.local/g/home/r/{slug}")
    monkeypatch.setattr(app_module.ha_notify, "notify", MagicMock())

    asyncio.run(app_module._process_import("https://example.com/rezepte/apfelkuchen"))

    generate_mock.assert_not_called()
    get_mock.assert_not_called()
    set_tags_mock.assert_called_once_with("apfelkuchen", ["auto-import"])
