"""Tests zu den Review-A8-Befunden 1 und 2 (recipe-import-plan.md, Auftragsdetails
A13). Befund 3 (Wettlauf zwischen `find` und `start`) hat eine eigene Testdatei,
`test_store_race.py`, weil er in `store.py` entsteht und dort geschlossen wird.
Befund 4 (`yt-dlp --output`) ist eine live verifizierte Annahme ohne Codeänderung,
siehe Kommentar in `sources/youtube.py`.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import app as app_module
from classify import normalize_url, url_hash
from schema import Recipe


def test_failed_set_tags_does_not_mark_import_failed_or_duplicate(monkeypatch, isolated_store):
    """Befund 1: `create_from_jsonld()` gelingt, `set_tags()` scheitert danach - das
    Rezept existiert in Mealie bereits. Der Import muss trotzdem als "done" gelten
    (Erfolgsmeldung nach DESIGN.md §7), und ein zweiter Anlauf zur selben URL darf
    `create_from_jsonld()` nicht ein zweites Mal aufrufen - das wäre die Doppelanlage,
    die Befund 1 beschreibt. Ohne die Reparatur landet der Eintrag nach dem
    set_tags()-Fehlschlag auf "failed", und der zweite Anlauf legt das Rezept erneut an."""
    monkeypatch.setattr(app_module, "classify", lambda url: "youtube")

    fetch_result = MagicMock(text="Rezepttext", title="Titel")
    monkeypatch.setattr(app_module.youtube, "fetch", lambda url: fetch_result)

    recipe = Recipe(
        name="Käsespätzle",
        recipeIngredient=["500 g Spätzle"],
        recipeInstructions=["Anbraten."],
    )
    monkeypatch.setattr(app_module, "extract_recipe", lambda text, url: recipe)

    create_mock = MagicMock(return_value="kaesespaetzle")
    monkeypatch.setattr(app_module.mealie_client, "create_from_jsonld", create_mock)
    monkeypatch.setattr(
        app_module.mealie_client, "set_tags", MagicMock(side_effect=RuntimeError("Mealie 500"))
    )
    monkeypatch.setattr(app_module.mealie_client, "recipe_link", lambda slug: f"http://mealie.local/g/home/r/{slug}")
    notify_mock = MagicMock()
    monkeypatch.setattr(app_module.ha_notify, "notify", notify_mock)

    url = "https://www.youtube.com/watch?v=abc12345678"
    h = url_hash(normalize_url(url))

    asyncio.run(app_module._process_import(url))

    entry = isolated_store.find(h)
    assert entry["status"] == "done"
    assert entry["slug"] == "kaesespaetzle"
    # Erfolgsmeldung, kein "Import fehlgeschlagen" - siehe DESIGN.md §7.
    assert notify_mock.call_args.args[0] == "Rezept angelegt"

    asyncio.run(app_module._process_import(url))
    assert create_mock.call_count == 1


def test_success_notification_uses_recipe_name_not_slug(monkeypatch, isolated_store):
    """Befund 2: auf dem Weg über `mealie_client.import_url()` bleibt `recipe` None.
    Die Rückmeldung muss den Rezeptnamen aus `get_recipe_name()` tragen, nicht den
    Slug - DESIGN.md §7 verlangt `<Name>`, nicht `<Slug>`."""
    monkeypatch.setattr(app_module, "classify", lambda url: "site")
    monkeypatch.setattr(
        app_module.mealie_client, "import_url", MagicMock(return_value="kartoffel-suppe-mit-wurst-3")
    )
    monkeypatch.setattr(app_module.mealie_client, "set_tags", MagicMock())
    # A16: die Platzhalter-Prüfung läuft vor get_recipe_name(); ohne diesen Mock würde
    # der echte get_recipe() einen Netzzugriff versuchen (DESIGN.md §12 verbietet das).
    monkeypatch.setattr(app_module.mealie_client, "get_recipe", lambda slug: {})
    monkeypatch.setattr(app_module.mealie_client, "is_placeholder", lambda recipe: False)
    monkeypatch.setattr(
        app_module.mealie_client, "get_recipe_name", MagicMock(return_value="Kartoffelsuppe mit Wurst")
    )
    monkeypatch.setattr(app_module.mealie_client, "recipe_link", lambda slug: f"http://mealie.local/g/home/r/{slug}")
    notify_mock = MagicMock()
    monkeypatch.setattr(app_module.ha_notify, "notify", notify_mock)

    url = "https://example.com/rezepte/kartoffelsuppe"

    asyncio.run(app_module._process_import(url))

    title = notify_mock.call_args.args[1]
    assert title == "Kartoffelsuppe mit Wurst"
    assert title != "kartoffel-suppe-mit-wurst-3"
