"""SQLite-Ablagestatus für Importe, ein Eintrag je normalisierter URL (§6 Ablauf).

Dient der Idempotenz: dieselbe URL löst höchstens einen Mealie-Aufruf aus (§12,
Test 5), und ein `pending`-Eintrag übersteht einen Neustart des Containers, weil
`app.py` ihn beim Start erneut über `pending()` einsammelt (§6, `src/app.py`).
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from config import DB_PATH

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
  url_hash   TEXT PRIMARY KEY,
  url        TEXT NOT NULL,
  status     TEXT NOT NULL,      -- pending | done | failed
  slug       TEXT,
  title      TEXT,
  error      TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def init() -> None:
    with _connect() as conn:
        conn.execute(SCHEMA)
        conn.commit()
    log.info("store.init: Tabelle imports unter %s bereit", DB_PATH)


def find(url_hash: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM imports WHERE url_hash = ?", (url_hash,)).fetchone()
    return dict(row) if row else None


def start(url_hash: str, url: str) -> bool:
    """Legt einen neuen `pending`-Eintrag an, oder setzt einen vorhandenen
    `failed`-Eintrag zurück auf `pending` - ein erneuter Versuch derselben URL muss
    möglich sein (§6). Auf einem `done`-Eintrag wird laut Ablauf Schritt 2 gar nicht
    erst gestartet; das prüft der Aufrufer über find(), nicht diese Funktion.

    Gibt True zurück, wenn dieser Aufruf den Eintrag jetzt exklusiv besitzt, sonst
    False. Das INSERT ... ON CONFLICT ... WHERE ist die Reparatur zu Review-A8-Befund
    3 (Wettlauf zwischen `find` und `start`, 2026-08-23): zwei fast gleichzeitige
    Anfragen zur selben URL lesen mit `find()` beide `status != "done"`, bevor eine von
    beiden hier ankommt. Statt "lesen, dann schreiben" entscheidet jetzt ein einziger
    atomarer INSERT: die WHERE-Klausel lässt die Aktualisierung nur zu, wenn der
    bestehende Eintrag `failed` ist. Ein bereits `pending`-Eintrag (die zweite,
    überflüssige Anfrage) bleibt unangetastet, `cur.rowcount` ist dann 0, und der
    Aufrufer bricht ab, statt ein zweites Mal durch die ganze Kette zu laufen. Live in
    diesem Prozess mit `sqlite3` nachgemessen: `rowcount` ist 1 bei Neuanlage und beim
    Zurücksetzen eines `failed`-Eintrags, 0 wenn die WHERE-Bedingung nicht zutrifft."""
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO imports (url_hash, url, status, created_at, updated_at)
            VALUES (?, ?, 'pending', ?, ?)
            ON CONFLICT(url_hash) DO UPDATE SET
                status = 'pending',
                error = NULL,
                updated_at = excluded.updated_at
            WHERE imports.status = 'failed'
            """,
            (url_hash, url, now, now),
        )
        conn.commit()
        claimed = cur.rowcount > 0
    if claimed:
        log.info("store.start(%s) url=%s -> pending", url_hash, url)
    else:
        log.info("store.start(%s) url=%s bereits in Arbeit oder fertig, kein zweiter Anlauf", url_hash, url)
    return claimed


def finish(url_hash: str, slug: str, title: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE imports SET status = 'done', slug = ?, title = ?, error = NULL, "
            "updated_at = ? WHERE url_hash = ?",
            (slug, title, _now(), url_hash),
        )
        conn.commit()
    log.info("store.finish(%s) slug=%s title=%r -> done", url_hash, slug, title)


def fail(url_hash: str, error: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE imports SET status = 'failed', error = ?, updated_at = ? WHERE url_hash = ?",
            (error, _now(), url_hash),
        )
        conn.commit()
    log.info("store.fail(%s) error=%s -> failed", url_hash, error)


def pending() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM imports WHERE status = 'pending'").fetchall()
    return [dict(row) for row in rows]


def recent_count(seconds: int) -> int:
    """Zahl der Einträge, deren created_at innerhalb der letzten `seconds` Sekunden
    liegt - Grundlage der Ratenbegrenzung in §11. ISO-8601-Zeitstempel mit fester
    UTC-Zone vergleichen sich lexikographisch wie numerisch, ein Vergleich als String
    reicht deshalb ohne SQLite-Datumsfunktionen."""
    cutoff = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM imports WHERE created_at >= ?", (cutoff,)
        ).fetchone()
    return row["n"]
