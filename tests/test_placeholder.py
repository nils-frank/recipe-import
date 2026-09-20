"""`mealie_client.import_url()` legt für eine erreichbare Seite ohne Rezept kein `None`
an, sondern ein Platzhalter-Rezept (Titel der Seite, Sentinel-Zutat, Sentinel-Schritt) -
live gegen eine echte Mealie-Instanz verifiziert (siehe `mealie_client.py`).

Die beiden Rezept-Antworten unten sind wörtlich die live abgefragten Formen: die
Platzhalter-Form stammt aus `https://de.wikipedia.org/wiki/Kartoffel`, die reale Form
(gekürzt auf die für `is_placeholder` relevanten Felder) aus der Chefkoch-Testseite in
`test_files/test_url.txt`.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import app as app_module
import mealie_client
from schema import Recipe

PLACEHOLDER_RECIPE = {
    "name": "Kartoffel – Wikipedia",  # noqa: RUF001 - live erfasster Mealie-Titel, EN DASH gehört zur Probe
    "recipeIngredient": [
        {
            "quantity": 0.0,
            "unit": None,
            "food": None,
            "note": "Could not detect ingredients",
            "display": "Could not detect ingredients",
        }
    ],
    "recipeInstructions": [
        {"title": "", "summary": "", "text": "Could not detect instructions"}
    ],
}

REAL_RECIPE = {
    "name": "Apfel - Käsekuchen vom Blech von Bärchenknutscher",
    "recipeIngredient": [
        {"quantity": 0.0, "unit": None, "food": None, "note": "360 g Mehl", "display": "360 g Mehl"},
        {"quantity": 0.0, "unit": None, "food": None, "note": "175 g Zucker", "display": "175 g Zucker"},
    ],
    "recipeInstructions": [
        {"title": "", "summary": "", "text": "Aus 350g Mehl, 175g Zucker ... einen glatten Teig kneten."},
    ],
}


def test_is_placeholder_true_for_sentinel_recipe():
    assert mealie_client.is_placeholder(PLACEHOLDER_RECIPE) is True


def test_is_placeholder_false_for_real_recipe():
    assert mealie_client.is_placeholder(REAL_RECIPE) is False


def test_is_placeholder_true_for_genuinely_empty_lists():
    """Verteidigend mitgeprüft (DESIGN.md §4/§6-Geist "weder Zutaten noch Schritte"),
    auch wenn die Live-Probe vom 2026-08-24 zeigt, dass Mealie tatsächlich den
    Sentinel-Eintrag liefert statt leerer Listen."""
    assert mealie_client.is_placeholder({"recipeIngredient": [], "recipeInstructions": []}) is True


def test_placeholder_is_deleted_and_import_falls_through_to_site_fetch(monkeypatch, isolated_store):
    """Ein Platzhalter zählt nicht als Erfolg von Stufe 5a (DESIGN.md §5): er wird
    gelöscht, und der Ablauf geht weiter zu `site.fetch()` plus LLM, wie bei einem
    regulären `None` aus `import_url()`."""
    monkeypatch.setattr(app_module, "classify", lambda url: "site")

    import_url_mock = MagicMock(return_value="kartoffel-wikipedia")
    monkeypatch.setattr(app_module.mealie_client, "import_url", import_url_mock)
    monkeypatch.setattr(app_module.mealie_client, "get_recipe", lambda slug: PLACEHOLDER_RECIPE)
    delete_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "delete_recipe", delete_mock)

    real_recipe = Recipe(
        name="Kartoffelgratin",
        recipeIngredient=["500 g Kartoffeln"],
        recipeInstructions=["Schichten und backen."],
    )
    monkeypatch.setattr(app_module.site, "fetch", lambda url: MagicMock(recipe=real_recipe, text=None))
    create_mock = MagicMock(return_value="kartoffelgratin")
    monkeypatch.setattr(app_module.mealie_client, "create_from_jsonld", create_mock)
    monkeypatch.setattr(app_module.mealie_client, "set_tags", MagicMock())
    monkeypatch.setattr(app_module.mealie_client, "recipe_link", lambda slug: f"http://mealie.local/g/home/r/{slug}")
    notify_mock = MagicMock()
    monkeypatch.setattr(app_module.ha_notify, "notify", notify_mock)

    url = "https://de.wikipedia.org/wiki/Kartoffel"
    asyncio.run(app_module._process_import(url))

    delete_mock.assert_called_once_with("kartoffel-wikipedia")
    create_mock.assert_called_once()
    assert notify_mock.call_args.args[0] == "Rezept angelegt"
    assert notify_mock.call_args.args[1] == "Kartoffelgratin"


def test_real_recipe_via_import_url_is_not_deleted(monkeypatch, isolated_store):
    """Gegenprobe: ein Rezept mit echten Zutaten und Schritten bleibt unangetastet,
    `delete_recipe` wird nicht aufgerufen und der bisherige Erfolgspfad (Name über
    `get_recipe_name`) läuft unverändert weiter."""
    monkeypatch.setattr(app_module, "classify", lambda url: "site")

    monkeypatch.setattr(
        app_module.mealie_client, "import_url", MagicMock(return_value="apfel-kaesekuchen")
    )
    monkeypatch.setattr(app_module.mealie_client, "get_recipe", lambda slug: REAL_RECIPE)
    delete_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "delete_recipe", delete_mock)
    monkeypatch.setattr(app_module.mealie_client, "set_tags", MagicMock())
    monkeypatch.setattr(
        app_module.mealie_client,
        "get_recipe_name",
        MagicMock(return_value="Apfel - Käsekuchen vom Blech von Bärchenknutscher"),
    )
    monkeypatch.setattr(app_module.mealie_client, "recipe_link", lambda slug: f"http://mealie.local/g/home/r/{slug}")
    notify_mock = MagicMock()
    monkeypatch.setattr(app_module.ha_notify, "notify", notify_mock)

    url = "https://www.chefkoch.de/rezepte/647121165936435/Apfel-Kaesekuchen-vom-Blech.html"
    asyncio.run(app_module._process_import(url))

    delete_mock.assert_not_called()
    assert notify_mock.call_args.args[0] == "Rezept angelegt"
    assert notify_mock.call_args.args[1] == "Apfel - Käsekuchen vom Blech von Bärchenknutscher"


def test_unreachable_placeholder_check_treats_slug_as_success(monkeypatch, isolated_store):
    """Kann die Platzhalter-Prüfung selbst nicht laufen (Mealie im Moment nicht
    erreichbar), darf das den sonst erfolgreichen `import_url()`-Aufruf nicht
    nachträglich zu einem Fehlschlag machen (DESIGN.md §5: `import_url` darf regulär
    danebengehen, aber ein bereits erfolgreicher Aufruf nicht zunichtegemacht werden)."""
    monkeypatch.setattr(app_module, "classify", lambda url: "site")

    monkeypatch.setattr(
        app_module.mealie_client, "import_url", MagicMock(return_value="irgendein-slug")
    )
    monkeypatch.setattr(
        app_module.mealie_client, "get_recipe", MagicMock(side_effect=mealie_client.MealieError("timeout"))
    )
    delete_mock = MagicMock()
    monkeypatch.setattr(app_module.mealie_client, "delete_recipe", delete_mock)
    monkeypatch.setattr(app_module.mealie_client, "set_tags", MagicMock())
    monkeypatch.setattr(
        app_module.mealie_client, "get_recipe_name", MagicMock(return_value="Irgendein Rezept")
    )
    monkeypatch.setattr(app_module.mealie_client, "recipe_link", lambda slug: f"http://mealie.local/g/home/r/{slug}")
    notify_mock = MagicMock()
    monkeypatch.setattr(app_module.ha_notify, "notify", notify_mock)

    url = "https://example.com/rezepte/irgendein-rezept"
    asyncio.run(app_module._process_import(url))

    delete_mock.assert_not_called()
    assert notify_mock.call_args.args[0] == "Rezept angelegt"
