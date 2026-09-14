"""Test 6 aus DESIGN.md §12: ein `pending`-Eintrag beim Start wird erneut verarbeitet.

`app.lifespan()` ruft `store.init()` und gibt danach jeden `pending()`-Eintrag erneut
in die Verarbeitung (DESIGN.md §6, `src/app.py`). `_run_import` wird durch einen
AsyncMock ersetzt - der Test prüft die Wiederaufnahme, nicht die Extraktion selbst.

Seit der Reparatur zu Review-A8-Befund 3 (2026-08-23, atomarer Claim in
`store.start()`) ruft der Wiederaufnahme-Pfad `_run_import(url_hash, url)` direkt auf,
nicht mehr `_process_import(url)`: Letzteres würde über `store.start()` erneut den
atomaren Claim versuchen, der auf einem schon `pending`-Eintrag scheitert (die
WHERE-Klausel lässt nur `failed -> pending` zu) und die Wiederaufnahme verhindern
würde. Beim Start ist dieser Container der einzige Besitzer der aus `store.pending()`
gelesenen Zeilen, der Claim entfällt hier bewusst - siehe `tests/test_store_race.py`
für den eigentlichen Wettlauf-Test.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import app as app_module
from classify import url_hash


def test_pending_entry_is_resumed_on_startup(monkeypatch, isolated_store):
    url = "https://example.com/rezepte/pfannkuchen"
    h = url_hash(url)
    isolated_store.start(h, url)
    assert isolated_store.find(h)["status"] == "pending"

    resumed = AsyncMock()
    monkeypatch.setattr(app_module, "_run_import", resumed)

    async def run() -> None:
        async with app_module.lifespan(app_module.app):
            # asyncio.create_task() plant nur - der Wiederaufnahme-Task braucht einen
            # Umlauf der Event-Loop, um tatsächlich zu starten.
            await asyncio.sleep(0.05)

    asyncio.run(run())

    resumed.assert_called_once_with(h, url)


def test_done_entry_is_not_resumed_on_startup(monkeypatch, isolated_store):
    url = "https://example.com/rezepte/erbsensuppe"
    h = url_hash(url)
    isolated_store.finish(h, "erbsensuppe", "Erbsensuppe")

    resumed = AsyncMock()
    monkeypatch.setattr(app_module, "_run_import", resumed)

    async def run() -> None:
        async with app_module.lifespan(app_module.app):
            await asyncio.sleep(0.05)

    asyncio.run(run())

    resumed.assert_not_called()
