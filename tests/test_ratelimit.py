"""Test 8 aus DESIGN.md §12: die Ratenbegrenzung greift beim konfigurierten Wert
(DESIGN.md §11: `store.recent_count(3600) >= RATE_LIMIT_PER_HOUR` -> 429).

`import_recipe` wird direkt aufgerufen statt über einen HTTP-Client, weil ein
Test-HTTP-Client (`fastapi.testclient`) `httpx` voraussetzt, das laut DESIGN.md §2
nicht Teil dieses Projekts ist. Ein einfaches Objekt mit `async def json()` erfüllt
die einzige Anforderung, die `import_recipe` an `request` stellt.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import BackgroundTasks, HTTPException

import app as app_module
import config
import payload
from classify import url_hash
from schema import Recipe
from sources import SourceResult


class _FakeRequest:
    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


def test_rate_limit_rejects_at_configured_threshold(monkeypatch):
    monkeypatch.setattr(app_module.store, "recent_count", lambda seconds: config.RATE_LIMIT_PER_HOUR)
    monkeypatch.setattr(app_module, "classify", lambda url: "site")

    request = _FakeRequest({"url": "https://example.com/rezepte/x"})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(app_module.import_recipe(request, BackgroundTasks()))

    assert exc_info.value.status_code == 429


def test_rate_limit_allows_below_configured_threshold(monkeypatch):
    monkeypatch.setattr(app_module.store, "recent_count", lambda seconds: config.RATE_LIMIT_PER_HOUR - 1)
    monkeypatch.setattr(app_module, "classify", lambda url: "site")
    started = []
    monkeypatch.setattr(app_module, "_process_import", lambda url: started.append(url))

    request = _FakeRequest({"url": "https://example.com/rezepte/y"})

    result = asyncio.run(app_module.import_recipe(request, BackgroundTasks()))

    assert result == {"status": "accepted"}


def test_a_due_retry_runs_although_the_hourly_limit_is_exhausted(
    monkeypatch, isolated_store, tmp_path
):
    """Aufgabe 8.1 (A20): die Begrenzung zählt angenommene Importe, keine Versuche.

    Ein wartender Eintrag ist längst angenommen und durch den Zähler gegangen; ein
    Versuch darf weder abgewiesen werden noch den Zähler ein zweites Mal erhöhen -
    `store.recent_count()` liest `created_at`, und kein Weg der Warteschlange fasst
    dieses Feld an.
    """
    monkeypatch.setattr(config, "QUEUE_PAYLOAD_DIR", str(tmp_path / "queue"))
    monkeypatch.setattr(config, "RATE_LIMIT_PER_HOUR", 1)

    url = "https://example.com/rezepte/wartend"
    h = url_hash(url)
    isolated_store.start(h, url)
    now = datetime.now(UTC)
    isolated_store.queue(h, (now - timedelta(minutes=1)).isoformat(), 1)

    # Die Stundengrenze ist bereits ausgeschöpft - durch diesen einen Eintrag selbst.
    assert isolated_store.recent_count(3600) >= config.RATE_LIMIT_PER_HOUR
    with pytest.raises(HTTPException) as exc_info:
        app_module._enforce_rate_limit(url)
    assert exc_info.value.status_code == 429

    recipe = Recipe(name="Wartend", recipeIngredient=["Mehl"], recipeInstructions=["Rühren"])
    monkeypatch.setattr(app_module.mealie_client, "import_url", lambda u: None)
    monkeypatch.setattr(
        app_module.site,
        "fetch",
        lambda u: SourceResult(recipe=recipe, text=None, title=None, stage="jsonld"),
    )
    monkeypatch.setattr(app_module.mealie_client, "create_from_jsonld", lambda data: "wartend")
    monkeypatch.setattr(app_module.mealie_client, "set_tags", lambda slug, tags: None)
    monkeypatch.setattr(app_module.mealie_client, "recipe_link", lambda slug: f"https://mealie/{slug}")
    monkeypatch.setattr(app_module.ha_notify, "notify", lambda *a, **kw: None)

    before = isolated_store.recent_count(3600)
    assert asyncio.run(app_module._run_due_once(now)) == 1

    assert isolated_store.find(h)["status"] == "done"
    assert isolated_store.recent_count(3600) == before
    assert payload.total_bytes() == 0
