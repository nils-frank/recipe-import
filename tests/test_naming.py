"""Tests zur Namensstufe (Feature A18, recipe-naming-plan.md).

Kein Netzzugriff (DESIGN.md §12): der einzige äussere Aufruf der Stufe ist `llm._post`,
und der wird hier ersetzt. Die Beispielnamen sind die real belegten Befunde aus dem Plan
§1 - der Chefkoch-Autorenzusatz aus A10 und der YouTube-Videotitel aus A15.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

import app as app_module
import config
import llm
import naming
from schema import Recipe

SCRAPED_NAME = "Apfel - Käsekuchen vom Blech von Bärchenknutscher"

MEALIE_RECIPE = {
    "name": SCRAPED_NAME,
    "recipeIngredient": [
        {"note": "360 g Mehl", "display": "360 g Mehl"},
        {"note": "175 g Zucker", "display": "175 g Zucker"},
        {"note": "1 kg Äpfel", "display": "1 kg Äpfel"},
    ],
    "recipeInstructions": [
        {"text": "Aus Mehl, Zucker und Butter einen glatten Teig kneten."},
        {"text": "Äpfel schälen, vierteln und auf dem Teig verteilen."},
        {"text": "Bei 180 Grad 50 Minuten backen."},
    ],
}


@pytest.fixture
def naming_on(monkeypatch):
    """Hebt die Abschaltung aus conftest.py für diesen einen Test wieder auf."""
    monkeypatch.setattr(config, "NAMING_ENABLED", True)


def _answer(name):
    return json.dumps({"name": name})


def _post_returning(name, captured=None):
    def fake_post(messages, timeout=None, schema=None, schema_name=None):
        if captured is not None:
            captured.append({"messages": messages, "timeout": timeout, "schema": schema})
        return _answer(name)

    return fake_post


# --- make_name: Nachprüfung aus Plan §4 ------------------------------------------


def test_strips_author_suffix(monkeypatch, naming_on):
    captured = []
    monkeypatch.setattr(llm, "_post", _post_returning("Apfel-Käsekuchen vom Blech", captured))

    result = naming.make_name(
        SCRAPED_NAME,
        "https://www.chefkoch.de/rezepte/647121165936435/Apfel-Kaesekuchen-vom-Blech.html",
        ["360 g Mehl", "1 kg Äpfel"],
        ["Teig kneten.", "Backen."],
    )

    assert result == "Apfel-Käsekuchen vom Blech"
    # Erzwungenes Ein-Feld-Schema und das kurze Zeitlimit aus Plan §2.2.
    assert captured[0]["schema"] == naming.NAME_SCHEMA
    assert captured[0]["timeout"] == naming.TIMEOUT_SECONDS
    user_part = captured[0]["messages"][1]["content"]
    assert f"Vorhandener Name: {SCRAPED_NAME}" in user_part
    assert "chefkoch.de" in user_part
    assert "1 kg Äpfel" in user_part


def test_unchanged_name_returns_none(monkeypatch, naming_on):
    """Der häufigste richtige Ausgang: der Name war schon gut. Kein Schreibzugriff."""
    monkeypatch.setattr(llm, "_post", _post_returning("Kaiserschmarrn"))

    assert naming.make_name("Kaiserschmarrn", "https://example.com/r", ["Mehl"], ["Backen."]) is None


def test_whitespace_only_difference_counts_as_unchanged(monkeypatch, naming_on):
    monkeypatch.setattr(llm, "_post", _post_returning("  Toast   Hawaii \n".replace("\n", " ")))

    assert naming.make_name("Toast Hawaii", "https://example.com/r", ["Toast"], ["Überbacken."]) is None


def test_missing_name_is_rendered_as_placeholder(monkeypatch, naming_on):
    """Kamerafoto ohne Überschrift: der Prompt sagt "(keiner)" statt einer leeren Zeile."""
    captured = []
    monkeypatch.setattr(llm, "_post", _post_returning("Kürbissuppe mit Ingwer", captured))

    result = naming.make_name("", "Foto", ["1 Hokkaido", "Ingwer"], ["Kochen und pürieren."])

    assert result == "Kürbissuppe mit Ingwer"
    assert "Vorhandener Name: (keiner)" in captured[0]["messages"][1]["content"]


@pytest.mark.parametrize(
    "answer",
    [
        "",
        "   ",
        "Erster Vorschlag\nZweiter Vorschlag",
        "x" * 81,
    ],
    ids=["leer", "nur-leerzeichen", "zeilenumbruch", "zu-lang"],
)
def test_unusable_answers_are_discarded(monkeypatch, naming_on, answer):
    monkeypatch.setattr(llm, "_post", _post_returning(answer))

    assert naming.make_name("Alter Name", "https://example.com/r", ["Mehl"], ["Backen."]) is None


def test_exactly_80_chars_is_accepted(monkeypatch, naming_on):
    long_name = "a" * 80
    monkeypatch.setattr(llm, "_post", _post_returning(long_name))

    assert naming.make_name("Alter Name", "https://example.com/r", ["Mehl"], ["Backen."]) == long_name


def test_llm_failure_never_raises(monkeypatch, naming_on):
    """Plan §2.2: jeder Fehlschlag lässt den alten Namen stehen, keine Ausnahme."""
    monkeypatch.setattr(llm, "_post", MagicMock(side_effect=llm.LlmError("Verbindung abgelehnt")))

    assert naming.make_name("Alter Name", "https://example.com/r", ["Mehl"], ["Backen."]) is None


def test_malformed_json_never_raises(monkeypatch, naming_on):
    monkeypatch.setattr(llm, "_post", lambda *a, **k: "kein JSON")

    assert naming.make_name("Alter Name", "https://example.com/r", ["Mehl"], ["Backen."]) is None


def test_request_is_truncated(monkeypatch, naming_on):
    """Plan §3.2: höchstens 30 Zutaten, 10 Schritte, 200 Zeichen je Schritt."""
    captured = []
    monkeypatch.setattr(llm, "_post", _post_returning("Grosses Gericht", captured))

    naming.make_name(
        "Alt",
        "https://example.com/r",
        [f"Zutat {i}" for i in range(50)],
        [f"Schritt {i} " + "x" * 500 for i in range(20)],
    )

    user_part = captured[0]["messages"][1]["content"]
    ingredients_block = user_part.split("Zutaten:\n")[1].split("\n\nZubereitung:")[0]
    instructions_block = user_part.split("Zubereitung:\n")[1]
    assert len(ingredients_block.splitlines()) == 30
    assert "Zutat 29" in ingredients_block and "Zutat 30" not in ingredients_block
    steps = instructions_block.splitlines()
    assert len(steps) == 10
    assert all(len(step) <= 200 for step in steps)


def test_disabled_switch_makes_no_call(monkeypatch):
    """NAMING_ENABLED=false: die Stufe entfällt vollständig (Plan §2.3, Abnahme 6)."""
    monkeypatch.setattr(config, "NAMING_ENABLED", False)
    post_mock = MagicMock()
    monkeypatch.setattr(llm, "_post", post_mock)

    assert naming.make_name("Alter Name", "https://example.com/r", ["Mehl"], ["Backen."]) is None
    post_mock.assert_not_called()


# --- Einbau in den Ablauf ---------------------------------------------------------


def _patch_common(monkeypatch):
    monkeypatch.setattr(app_module.mealie_client, "set_tags", MagicMock())
    monkeypatch.setattr(
        app_module.mealie_client, "recipe_link", lambda slug: f"http://mealie.local/g/home/r/{slug}"
    )
    notify_mock = MagicMock()
    monkeypatch.setattr(app_module.ha_notify, "notify", notify_mock)
    return notify_mock


def test_scraped_recipe_is_renamed_and_slug_is_followed(monkeypatch, naming_on, isolated_store):
    """Abnahme 1: der Autorenzusatz verschwindet. Mealie leitet den Slug dabei neu ab
    (live verifiziert am 2026-08-24, entgegen der Annahme in Plan §2.1); Tags, Link und
    store-Eintrag folgen deshalb dem neuen Slug."""
    monkeypatch.setattr(app_module, "classify", lambda url: "site")
    monkeypatch.setattr(
        app_module.mealie_client,
        "import_url",
        MagicMock(return_value="apfel-kaesekuchen-vom-blech-von-baerchenknutscher"),
    )
    monkeypatch.setattr(app_module.mealie_client, "get_recipe", lambda slug: MEALIE_RECIPE)
    monkeypatch.setattr(app_module.mealie_client, "delete_recipe", MagicMock())
    # Mealie leitet den Slug bei der Umbenennung neu ab - live verifiziert am
    # 2026-08-24, siehe mealie_client.rename. Der Mock bildet genau das nach.
    rename_mock = MagicMock(return_value="apfel-kaesekuchen-vom-blech")
    monkeypatch.setattr(app_module.mealie_client, "rename", rename_mock)
    monkeypatch.setattr(llm, "_post", _post_returning("Apfel-Käsekuchen vom Blech"))
    notify_mock = _patch_common(monkeypatch)
    set_tags_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "set_tags", set_tags_mock)

    url = "https://www.chefkoch.de/rezepte/647121165936435/Apfel-Kaesekuchen-vom-Blech.html"
    asyncio.run(app_module._process_import(url))

    old_slug = "apfel-kaesekuchen-vom-blech-von-baerchenknutscher"
    new_slug = "apfel-kaesekuchen-vom-blech"
    rename_mock.assert_called_once_with(old_slug, "Apfel-Käsekuchen vom Blech")
    # Ab der Umbenennung gilt nur noch der neue Slug: Tags, store-Eintrag und der Link
    # in der Push-Meldung müssen darauf zeigen, sonst laufen alle drei ins Leere.
    set_tags_mock.assert_called_once_with(new_slug, ["auto-import"])
    assert notify_mock.call_args.args[1] == "Apfel-Käsekuchen vom Blech"
    assert notify_mock.call_args.args[2].endswith(new_slug)
    entry = isolated_store.find(app_module.url_hash(app_module.normalize_url(url)))
    assert entry["slug"] == new_slug
    assert entry["title"] == "Apfel-Käsekuchen vom Blech"


def test_good_scraped_name_causes_no_patch(monkeypatch, naming_on, isolated_store):
    """Abnahme 4: ein bereits guter Name geht ohne Schreibzugriff durch."""
    monkeypatch.setattr(app_module, "classify", lambda url: "site")
    monkeypatch.setattr(app_module.mealie_client, "import_url", MagicMock(return_value="kaiserschmarrn"))
    monkeypatch.setattr(
        app_module.mealie_client, "get_recipe", lambda slug: dict(MEALIE_RECIPE, name="Kaiserschmarrn")
    )
    rename_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "rename", rename_mock)
    monkeypatch.setattr(llm, "_post", _post_returning("Kaiserschmarrn"))
    notify_mock = _patch_common(monkeypatch)

    asyncio.run(app_module._process_import("https://example.com/rezepte/kaiserschmarrn"))

    rename_mock.assert_not_called()
    assert notify_mock.call_args.args[1] == "Kaiserschmarrn"


def test_naming_failure_does_not_break_scraped_import(monkeypatch, naming_on, isolated_store):
    """Abnahme 5: fällt die Stufe aus, läuft der Import mit dem alten Namen durch."""
    monkeypatch.setattr(app_module, "classify", lambda url: "site")
    monkeypatch.setattr(app_module.mealie_client, "import_url", MagicMock(return_value="apfel-kaesekuchen"))
    monkeypatch.setattr(app_module.mealie_client, "get_recipe", lambda slug: MEALIE_RECIPE)
    monkeypatch.setattr(app_module.mealie_client, "rename", MagicMock())
    monkeypatch.setattr(llm, "_post", MagicMock(side_effect=llm.LlmError("Verbindung abgelehnt")))
    notify_mock = _patch_common(monkeypatch)

    asyncio.run(app_module._process_import("https://example.com/rezepte/apfel"))

    assert notify_mock.call_args.args[0] == "Rezept angelegt"
    assert notify_mock.call_args.args[1] == SCRAPED_NAME


def test_rename_failure_does_not_break_import(monkeypatch, naming_on, isolated_store):
    """Auch ein abgelehntes PATCH kostet nur den schönen Namen, nicht den Import."""
    monkeypatch.setattr(app_module, "classify", lambda url: "site")
    monkeypatch.setattr(app_module.mealie_client, "import_url", MagicMock(return_value="apfel-kaesekuchen"))
    monkeypatch.setattr(app_module.mealie_client, "get_recipe", lambda slug: MEALIE_RECIPE)
    monkeypatch.setattr(
        app_module.mealie_client,
        "rename",
        MagicMock(side_effect=app_module.mealie_client.MealieError("500: kaputt")),
    )
    monkeypatch.setattr(llm, "_post", _post_returning("Apfel-Käsekuchen vom Blech"))
    notify_mock = _patch_common(monkeypatch)
    set_tags_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "set_tags", set_tags_mock)

    asyncio.run(app_module._process_import("https://example.com/rezepte/apfel"))

    assert notify_mock.call_args.args[0] == "Rezept angelegt"
    assert notify_mock.call_args.args[1] == SCRAPED_NAME
    # Scheitert das PATCH, bleibt der ursprüngliche Slug gültig.
    set_tags_mock.assert_called_once_with("apfel-kaesekuchen", ["auto-import"])


def test_extracted_recipe_is_renamed_before_create(monkeypatch, naming_on, isolated_store):
    """Abnahme 2: der YouTube-Weg legt das Rezept gleich unter dem guten Namen an, der
    Slug entsteht damit aus dem guten Namen."""
    monkeypatch.setattr(app_module, "classify", lambda url: "youtube")
    monkeypatch.setattr(
        app_module.youtube, "fetch", lambda url: MagicMock(recipe=None, text="Untertiteltext")
    )
    extracted = Recipe(
        name="LIEBLINGS-PASTA Rezept meiner Familie 🍝",
        recipeIngredient=["500 g Spaghetti", "200 g Guanciale"],
        recipeInstructions=["Nudeln kochen.", "Mit Ei und Käse mischen."],
    )
    monkeypatch.setattr(app_module, "extract_recipe", lambda text, source: extracted)
    create_mock = MagicMock(return_value="spaghetti-carbonara")
    monkeypatch.setattr(app_module.mealie_client, "create_from_jsonld", create_mock)
    rename_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "rename", rename_mock)
    monkeypatch.setattr(llm, "_post", _post_returning("Spaghetti Carbonara"))
    notify_mock = _patch_common(monkeypatch)

    asyncio.run(app_module._process_import("https://www.youtube.com/watch?v=abcdefghijk"))

    # Kein PATCH: der Name steht schon im angelegten Rezept.
    rename_mock.assert_not_called()
    sent = create_mock.call_args.args[0]
    assert sent["name"] == "Spaghetti Carbonara"
    assert notify_mock.call_args.args[1] == "Spaghetti Carbonara"


def test_disabled_switch_leaves_scraped_path_without_llm(monkeypatch, isolated_store):
    """Abnahme 6: mit NAMING_ENABLED=false bleibt der import_url-Weg ohne LLM-Aufruf.
    Die Abschaltung kommt hier aus der autouse-Fixture in conftest.py."""
    monkeypatch.setattr(app_module, "classify", lambda url: "site")
    monkeypatch.setattr(app_module.mealie_client, "import_url", MagicMock(return_value="apfel-kaesekuchen"))
    monkeypatch.setattr(app_module.mealie_client, "get_recipe", lambda slug: MEALIE_RECIPE)
    rename_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "rename", rename_mock)
    post_mock = MagicMock()
    monkeypatch.setattr(llm, "_post", post_mock)
    notify_mock = _patch_common(monkeypatch)

    asyncio.run(app_module._process_import("https://example.com/rezepte/apfel"))

    post_mock.assert_not_called()
    rename_mock.assert_not_called()
    assert notify_mock.call_args.args[1] == SCRAPED_NAME


def test_recipe_texts_flattens_mealie_shape():
    ingredients, instructions = app_module.mealie_client.recipe_texts(MEALIE_RECIPE)

    assert ingredients == ["360 g Mehl", "175 g Zucker", "1 kg Äpfel"]
    assert instructions[0] == "Aus Mehl, Zucker und Butter einen glatten Teig kneten."
