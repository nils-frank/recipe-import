"""Test 5 aus DESIGN.md §12: derselbe URL-Hash zweimal erzeugt genau einen Mealie-Aufruf.

Ablauf-Schritt 2 (DESIGN.md §5) prüft vor jedem Import über `store.find`, ob die URL
schon `done` ist, und meldet dann nur den alten Link zurück statt Mealie erneut
aufzurufen. `mealie_client.import_url` wird gemockt, damit der Erfolgspfad ohne
`site.fetch`/LLM auskommt - der Test prüft die Idempotenz, nicht die Extraktion.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import app as app_module


def test_same_url_twice_calls_mealie_exactly_once(monkeypatch, isolated_store):
    monkeypatch.setattr(app_module, "classify", lambda url: "site")

    import_url_mock = MagicMock(return_value="apfelkuchen-mit-quark")
    monkeypatch.setattr(app_module.mealie_client, "import_url", import_url_mock)
    monkeypatch.setattr(app_module.mealie_client, "set_tags", MagicMock())
    # A16: nach import_url() folgt jetzt die Platzhalter-Prüfung (get_recipe/
    # is_placeholder), bevor der Slug als Erfolg gilt. Hier kein Platzhalter, sonst
    # würde dieser Test den Fallback auf site.fetch()/LLM auslösen, den er nicht mockt.
    monkeypatch.setattr(app_module.mealie_client, "get_recipe", lambda slug: {})
    monkeypatch.setattr(app_module.mealie_client, "is_placeholder", lambda recipe: False)
    # Der Weg über import_url() liefert kein Recipe-Objekt (recipe bleibt None), der
    # Name kommt seit der Reparatur zu Befund 2 (Review A8) aus get_recipe_name().
    monkeypatch.setattr(app_module.mealie_client, "get_recipe_name", lambda slug: "Apfelkuchen mit Quark")
    monkeypatch.setattr(app_module.mealie_client, "recipe_link", lambda slug: f"http://mealie.local/g/home/r/{slug}")
    notify_mock = MagicMock()
    monkeypatch.setattr(app_module.ha_notify, "notify", notify_mock)

    url = "https://www.backenmachtgluecklich.de/rezepte/apfelkuchen-mit-quark.html"

    asyncio.run(app_module._process_import(url))
    asyncio.run(app_module._process_import(url))

    assert import_url_mock.call_count == 1
    # Zweiter Aufruf meldet "schon vorhanden" statt zu scheitern, siehe DESIGN.md §7.
    assert notify_mock.call_count == 2
    second_call_title = notify_mock.call_args_list[1].args[0]
    assert second_call_title == "Rezept schon vorhanden"
