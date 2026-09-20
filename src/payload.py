"""Aufbewahrte Bytes hochgeladener Dateien für geparkte Importe (A20,
openspec/changes/add-transient-retry-queue).

Bisher speichert der Dienst von einem Upload nur den Inhalts-Hash (DESIGN.md §5),
weshalb ein Datei-Import einen Neustart nicht übersteht. Wartet ein Import auf einen
späteren Versuch, müssen die Bytes aber da sein - sonst hiesse "ich versuche es später
noch einmal" in Wahrheit "bitte noch einmal teilen".

Ablage: ein Verzeichnis je `url_hash` unter `QUEUE_PAYLOAD_DIR`, darin eine Datei je
Upload, durchnummeriert. Der ursprüngliche Dateiname und der Inhaltstyp stehen **nicht**
im Dateinamen, sondern in der JSON-Beschreibung, die in der Spalte `payload` der Zeile
liegt: ein Dateiname aus dem Teilen-Menü ist fremde Eingabe, und aus fremder Eingabe
einen Pfad zu bauen ist der Weg zu `../`.

Lebensdauer: geschrieben beim Parken, gelesen beim Wiederholen, gelöscht beim Übergang
in einen Endzustand (fertig, endgültig gescheitert, aufgegeben). Weil ein Absturz
zwischen "Zeile aktualisiert" und "Dateien gelöscht" liegen kann, räumt `sweep()` beim
Start jedes Verzeichnis ab, zu dem keine wartende Zeile mehr gehört.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import config
from sources.document import Upload

log = logging.getLogger(__name__)


def _root() -> Path:
    """Zur Laufzeit gelesen, nicht beim Import: so wirkt ein in der Testreihe
    umgesetzter `QUEUE_PAYLOAD_DIR` auch hier."""
    return Path(config.QUEUE_PAYLOAD_DIR)


def _directory(url_hash: str) -> Path:
    return _root() / url_hash


def total_bytes() -> int:
    """Summe aller aufbewahrten Bytes. Grundlage der Budgetprüfung vor dem Parken."""
    root = _root()
    if not root.is_dir():
        return 0
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


def fits_in_budget(uploads: list[Upload]) -> bool:
    """Ob diese Uploads zusätzlich zum bereits Aufbewahrten noch in
    `QUEUE_PAYLOAD_MAX_MB` passen.

    Geprüft wird **vor** dem Parken. Ist kein Platz, scheitert dieser Import endgültig,
    statt den Inhalt eines anderen wartenden Eintrags zu verdrängen - verdrängter Inhalt
    hiesse ein Eintrag, der nie mehr laufen kann (design.md)."""
    limit = int(config.QUEUE_PAYLOAD_MAX_MB * 1024 * 1024)
    needed = sum(len(u.data) for u in uploads)
    return total_bytes() + needed <= limit


def save(url_hash: str, uploads: list[Upload]) -> str:
    """Legt die Bytes ab und gibt die JSON-Beschreibung für die Spalte `payload`
    zurück. Ein bereits vorhandenes Verzeichnis desselben Hashes wird ersetzt: dieselben
    Bytes ergeben denselben Hash, der Inhalt ist also derselbe."""
    directory = _directory(url_hash)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)

    files = []
    for index, upload in enumerate(uploads):
        stored = f"{index:02d}.bin"
        (directory / stored).write_bytes(upload.data)
        files.append(
            {"stored": stored, "filename": upload.filename, "content_type": upload.content_type}
        )

    log.info("payload.save(%s): %d Datei(en) in %s", url_hash, len(files), directory)
    return json.dumps({"files": files})


def load(url_hash: str, payload: str | None) -> list[Upload] | None:
    """Liest die aufbewahrten Uploads zurück, mit ursprünglichem Namen und Inhaltstyp.

    `None`, wenn die Beschreibung fehlt, unlesbar ist oder eine Datei nicht mehr auf der
    Platte liegt. Der Aufrufer behandelt das wie einen Datei-Import ohne aufbewahrte
    Bytes und bittet den Menschen, die Datei noch einmal zu teilen - das ist die
    Rückmeldung, die es dafür schon gibt."""
    if not payload:
        return None

    try:
        entries = json.loads(payload)["files"]
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("payload.load(%s): Beschreibung unlesbar: %s", url_hash, exc)
        return None

    directory = _directory(url_hash)
    uploads = []
    for entry in entries:
        path = directory / str(entry.get("stored", ""))
        # Kein Pfad aus fremder Eingabe: `stored` stammt zwar aus der eigenen
        # Datenbank, aber ein Name mit `/` oder `..` darf trotzdem nicht aus dem
        # Verzeichnis führen.
        if path.parent != directory or not path.is_file():
            log.warning("payload.load(%s): %s fehlt, keine Wiederaufnahme", url_hash, path)
            return None
        uploads.append(
            Upload(
                filename=entry.get("filename") or "unbenannt",
                content_type=entry.get("content_type") or "",
                data=path.read_bytes(),
            )
        )

    return uploads or None


def delete(url_hash: str) -> None:
    """Räumt das Verzeichnis eines Hashes ab. Wirkungslos, wenn es nichts gibt - der
    Aufruf steht an jedem Endzustand, auch bei URL-Importen ohne Uploads."""
    directory = _directory(url_hash)
    if not directory.exists():
        return
    shutil.rmtree(directory, ignore_errors=True)
    log.info("payload.delete(%s): %s entfernt", url_hash, directory)


def sweep(keep: set[str]) -> list[str]:
    """Entfernt jedes Verzeichnis, dessen Hash nicht in `keep` steht, und gibt die
    entfernten Hashes zurück.

    Läuft beim Start (DESIGN.md §6): ein Absturz zwischen dem Endzustand der Zeile und
    dem Löschen der Dateien hinterlässt sonst Bytes, die niemand mehr abholt."""
    root = _root()
    if not root.is_dir():
        return []

    removed = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name in keep:
            continue
        shutil.rmtree(directory, ignore_errors=True)
        removed.append(directory.name)

    if removed:
        log.info("payload.sweep: %d verwaiste Verzeichnisse entfernt: %s", len(removed), ", ".join(removed))
    return removed
