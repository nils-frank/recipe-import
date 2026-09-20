"""SQLite-Ablagestatus für Importe, ein Eintrag je normalisierter URL (§6 Ablauf).

Dient der Idempotenz: dieselbe URL löst höchstens einen Mealie-Aufruf aus (§12,
Test 5), und ein `pending`-Eintrag übersteht einen Neustart des Containers, weil
`app.py` ihn beim Start erneut über `pending()` einsammelt (§6, `src/app.py`).

Seit A20 (openspec/changes/add-transient-retry-queue) trägt dieselbe Tabelle die
Warteschlange für vorübergehend gescheiterte Importe: der Status `queued` mit
`due_at`, `attempts` und `payload`. Bewusst keine zweite Tabelle - jede bestehende
Zusicherung (der atomare Claim in `start()`, `_notify_if_done`, die Ratenbegrenzung)
müsste sonst zwei Tabellen befragen, um den Stand einer Quelle zu kennen.
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
  status     TEXT NOT NULL,      -- pending | queued | done | failed
  slug       TEXT,
  title      TEXT,
  error      TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  attempts   INTEGER NOT NULL DEFAULT 0,
  due_at     TEXT,
  payload    TEXT
);
"""

# Nachtrag der Warteschlangen-Spalten auf einer Datenbank, die vor A20 angelegt wurde
# (openspec/changes/add-transient-retry-queue). Rein additiv: vorhandene Zeilen behalten
# ihren Status und bekommen attempts = 0, due_at = NULL, payload = NULL. Eine ältere
# Fassung des Dienstes ignoriert die Spalten wieder, deshalb braucht es keinen
# Rückweg - zu einer `queued`-Zeile, die sie nicht versteht, siehe design.md.
_MIGRATIONS = {
    "attempts": "ALTER TABLE imports ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
    "due_at": "ALTER TABLE imports ADD COLUMN due_at TEXT",
    "payload": "ALTER TABLE imports ADD COLUMN payload TEXT",
}


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
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(imports)")}
        for column, statement in _MIGRATIONS.items():
            if column not in existing:
                log.info("store.init: ergaenze Spalte %s", column)
                conn.execute(statement)
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
    """Endzustand `done`. Räumt zugleich die Warteschlangenfelder ab (A20): ein
    Eintrag, der aus `queued` heraus fertig geworden ist, trägt sonst eine Fälligkeit
    weiter, die niemand mehr einlöst."""
    with _connect() as conn:
        conn.execute(
            "UPDATE imports SET status = 'done', slug = ?, title = ?, error = NULL,"
            " due_at = NULL, attempts = 0, updated_at = ? WHERE url_hash = ?",
            (slug, title, _now(), url_hash),
        )
        conn.commit()
    log.info("store.finish(%s) slug=%s title=%r -> done", url_hash, slug, title)


def fail(url_hash: str, error: str) -> None:
    """Endzustand `failed`, endgültig oder nach aufgegebenen Wiederholungen. Räumt die
    Warteschlangenfelder ab, aus demselben Grund wie `finish()`."""
    with _connect() as conn:
        conn.execute(
            "UPDATE imports SET status = 'failed', error = ?,"
            " due_at = NULL, attempts = 0, updated_at = ? WHERE url_hash = ?",
            (error, _now(), url_hash),
        )
        conn.commit()
    log.info("store.fail(%s) error=%s -> failed", url_hash, error)


def queue(url_hash: str, due_at: str, attempts: int, payload: str | None = None) -> None:
    """Parkt einen Eintrag als `queued` mit Fälligkeit und Versuchszähler (A20).

    `due_at` ist ein ISO-8601-Zeitstempel in UTC, wie `created_at`/`updated_at` - so
    vergleicht ihn `due()` als Zeichenkette. `payload` trägt die JSON-Beschreibung
    aufbewahrter Uploads oder `None` für einen URL-Import.

    Die Zeile existiert an dieser Stelle immer: geparkt wird nur, was zuvor über
    `start()` oder `claim_due()` als `pending` übernommen wurde."""
    with _connect() as conn:
        conn.execute(
            "UPDATE imports SET status = 'queued', due_at = ?, attempts = ?, payload = ?,"
            " error = NULL, updated_at = ? WHERE url_hash = ?",
            (due_at, attempts, payload, _now(), url_hash),
        )
        conn.commit()
    log.info("store.queue(%s) attempts=%d due_at=%s -> queued", url_hash, attempts, due_at)


def due(now: str) -> list[dict]:
    """Alle `queued`-Einträge, deren Fälligkeit erreicht ist. `now` wird übergeben
    statt hier gelesen, damit die Zeitplanung ohne Wanduhr prüfbar bleibt (DESIGN.md
    §12). Älteste Fälligkeit zuerst: eine lange wartende Zeile soll nicht hinter einer
    gerade erst geparkten zurückstehen."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM imports WHERE status = 'queued' AND due_at IS NOT NULL"
            " AND due_at <= ? ORDER BY due_at",
            (now,),
        ).fetchall()
    return [dict(row) for row in rows]


def claim_due(url_hash: str) -> bool:
    """Übernimmt eine fällige Zeile zur Verarbeitung: `queued` -> `pending`, atomar.

    Dasselbe Muster wie `start()` und aus demselben Grund: zwischen `due()` und dem
    Beginn der Arbeit kann ein erneut geteilter Link dieselbe Zeile berühren. Ein
    einziges UPDATE mit Statusbedingung und `rowcount` entscheidet, wem sie gehört;
    `False` heisst "jemand anders hat sie", nicht "Fehler".

    `created_at` bleibt unangetastet - daran hängt `recent_count()`, und ein Versuch
    ist kein neu angenommener Import (Ratenbegrenzung, DESIGN.md §11)."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE imports SET status = 'pending', updated_at = ?"
            " WHERE url_hash = ? AND status = 'queued'",
            (_now(), url_hash),
        )
        conn.commit()
        claimed = cur.rowcount > 0
    if claimed:
        log.info("store.claim_due(%s) -> pending", url_hash)
    else:
        log.info("store.claim_due(%s) nicht mehr queued, kein zweiter Anlauf", url_hash)
    return claimed


def queued() -> list[dict]:
    """Alle wartenden Einträge, unabhängig von der Fälligkeit - Grundlage des
    Aufräumens verwaister Upload-Verzeichnisse beim Start (A20)."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM imports WHERE status = 'queued'").fetchall()
    return [dict(row) for row in rows]


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
