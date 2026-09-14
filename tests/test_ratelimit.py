"""Test 8 aus DESIGN.md §12: die Ratenbegrenzung greift beim konfigurierten Wert
(DESIGN.md §11: `store.recent_count(3600) >= RATE_LIMIT_PER_HOUR` -> 429).

`import_recipe` wird direkt aufgerufen statt über einen HTTP-Client, weil ein
Test-HTTP-Client (`fastapi.testclient`) `httpx` voraussetzt, das laut DESIGN.md §2
nicht Teil dieses Projekts ist. Ein einfaches Objekt mit `async def json()` erfüllt
die einzige Anforderung, die `import_recipe` an `request` stellt.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

import app as app_module
import config


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
