"""Warteschlangenzustand in `store` (Aufgaben 2.1 bis 2.3, A20).

Die Zeitangaben gehen als Argument hinein statt aus der Wanduhr zu kommen - DESIGN.md
§12 verlangt eine Testreihe ohne Zeitabhängigkeit, und `store.due()` ist genau deshalb
so geschnitten.
"""
from __future__ import annotations

import sqlite3

import store as store_module

# Die Tabelle, wie sie vor A20 aussah. Grundlage des Migrationstests: eine bestehende
# Installation hat genau diese Spalten auf der Platte.
_OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
  url_hash   TEXT PRIMARY KEY,
  url        TEXT NOT NULL,
  status     TEXT NOT NULL,
  slug       TEXT,
  title      TEXT,
  error      TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


def _columns(path: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [row[1] for row in conn.execute("PRAGMA table_info(imports)")]
    finally:
        conn.close()


def test_init_migrates_an_old_database_and_is_idempotent(tmp_path, monkeypatch):
    path = str(tmp_path / "alt.db")
    conn = sqlite3.connect(path)
    conn.execute(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO imports (url_hash, url, status, created_at, updated_at)"
        " VALUES ('alt1', 'https://example.com/alt', 'done', '2026-01-01T00:00:00+00:00',"
        " '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(store_module, "DB_PATH", path)
    store_module.init()
    store_module.init()

    columns = _columns(path)
    for name in ("attempts", "due_at", "payload"):
        assert columns.count(name) == 1

    row = store_module.find("alt1")
    assert row["status"] == "done"
    assert row["attempts"] == 0
    assert row["due_at"] is None
    assert row["payload"] is None


def test_queue_parks_a_row_with_due_time_and_attempts(isolated_store):
    isolated_store.start("h1", "https://example.com/rezept")

    isolated_store.queue("h1", "2026-09-20T12:00:00+00:00", 1, '{"files": []}')

    row = isolated_store.find("h1")
    assert row["status"] == "queued"
    assert row["due_at"] == "2026-09-20T12:00:00+00:00"
    assert row["attempts"] == 1
    assert row["payload"] == '{"files": []}'


def test_due_selects_only_reached_due_times_oldest_first(isolated_store):
    isolated_store.start("spaet", "https://example.com/spaet")
    isolated_store.queue("spaet", "2026-09-20T13:00:00+00:00", 1)
    isolated_store.start("frueh", "https://example.com/frueh")
    isolated_store.queue("frueh", "2026-09-20T11:00:00+00:00", 1)
    isolated_store.start("gleich", "https://example.com/gleich")
    isolated_store.queue("gleich", "2026-09-20T12:00:00+00:00", 2)

    hashes = [row["url_hash"] for row in isolated_store.due("2026-09-20T12:00:00+00:00")]

    # "gleich" ist auf die Sekunde fällig und gehört dazu, "spaet" noch nicht.
    assert hashes == ["frueh", "gleich"]


def test_due_ignores_rows_that_are_not_queued(isolated_store):
    isolated_store.start("laeuft", "https://example.com/laeuft")

    assert isolated_store.due("2999-01-01T00:00:00+00:00") == []


def test_claim_due_moves_queued_to_pending_exactly_once(isolated_store):
    isolated_store.start("h2", "https://example.com/zwei")
    isolated_store.queue("h2", "2026-09-20T12:00:00+00:00", 1)

    assert isolated_store.claim_due("h2") is True
    assert isolated_store.find("h2")["status"] == "pending"
    # Der zweite Anlauf auf dieselbe Zeile: der Scheduler und ein erneut geteilter Link
    # dürfen sie nicht beide besitzen.
    assert isolated_store.claim_due("h2") is False


def test_claim_due_keeps_created_at(isolated_store):
    """Die Ratenbegrenzung zählt `created_at` (DESIGN.md §11). Ein Wiederholungsversuch
    ist kein neu angenommener Import."""
    isolated_store.start("h3", "https://example.com/drei")
    created = isolated_store.find("h3")["created_at"]
    isolated_store.queue("h3", "2026-09-20T12:00:00+00:00", 1)

    isolated_store.claim_due("h3")

    assert isolated_store.find("h3")["created_at"] == created


def test_start_does_not_take_over_a_queued_row(isolated_store):
    """Dieselbe Quelle erneut geteilt, während sie wartet: kein zweiter Eintrag und
    kein zweiter Durchlauf."""
    isolated_store.start("h4", "https://example.com/vier")
    isolated_store.queue("h4", "2026-09-20T12:00:00+00:00", 1)

    assert isolated_store.start("h4", "https://example.com/vier") is False
    assert isolated_store.find("h4")["status"] == "queued"


def test_finish_clears_the_queue_fields(isolated_store):
    isolated_store.start("h5", "https://example.com/fuenf")
    isolated_store.queue("h5", "2026-09-20T12:00:00+00:00", 2, '{"files": []}')
    isolated_store.claim_due("h5")

    isolated_store.finish("h5", "fuenf", "Fünf")

    row = isolated_store.find("h5")
    assert row["status"] == "done"
    assert row["due_at"] is None
    assert row["attempts"] == 0
    assert isolated_store.due("2999-01-01T00:00:00+00:00") == []


def test_fail_clears_the_queue_fields(isolated_store):
    isolated_store.start("h6", "https://example.com/sechs")
    isolated_store.queue("h6", "2026-09-20T12:00:00+00:00", 3)

    isolated_store.fail("h6", "Auch nach mehreren Versuchen hat es nicht geklappt.")

    row = isolated_store.find("h6")
    assert row["status"] == "failed"
    assert row["due_at"] is None
    assert row["attempts"] == 0


def test_queued_lists_every_waiting_row(isolated_store):
    isolated_store.start("a", "https://example.com/a")
    isolated_store.queue("a", "2999-01-01T00:00:00+00:00", 1)
    isolated_store.start("b", "https://example.com/b")

    assert [row["url_hash"] for row in isolated_store.queued()] == ["a"]
