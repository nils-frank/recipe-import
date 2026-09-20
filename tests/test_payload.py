"""Aufbewahrte Uploads geparkter Importe (Aufgabe 5.1, A20)."""
from __future__ import annotations

import json

import pytest

import config
import payload
from sources.document import Upload


@pytest.fixture
def payload_dir(tmp_path, monkeypatch):
    directory = tmp_path / "queue"
    monkeypatch.setattr(config, "QUEUE_PAYLOAD_DIR", str(directory))
    return directory


def _uploads() -> list[Upload]:
    return [
        Upload(filename="rezept.jpg", content_type="image/jpeg", data=b"\xff\xd8bild"),
        Upload(filename="seite 2.pdf", content_type="application/pdf", data=b"%PDF-1.4"),
    ]


def test_round_trip_keeps_filename_and_content_type(payload_dir):
    meta = payload.save("abc123", _uploads())

    restored = payload.load("abc123", meta)

    assert [(u.filename, u.content_type, u.data) for u in restored] == [
        (u.filename, u.content_type, u.data) for u in _uploads()
    ]


def test_files_live_in_a_directory_named_after_the_hash(payload_dir):
    payload.save("abc123", _uploads())

    assert sorted(p.name for p in (payload_dir / "abc123").iterdir()) == ["00.bin", "01.bin"]


def test_the_original_filename_never_becomes_a_path(payload_dir):
    """Ein Dateiname aus dem Teilen-Menü ist fremde Eingabe."""
    evil = [Upload(filename="../../etc/passwd", content_type="text/plain", data=b"x")]

    meta = payload.save("abc123", evil)

    assert list((payload_dir / "abc123").iterdir()) == [payload_dir / "abc123" / "00.bin"]
    assert payload.load("abc123", meta)[0].filename == "../../etc/passwd"


def test_saving_the_same_hash_twice_replaces_the_content(payload_dir):
    payload.save("abc123", _uploads())
    meta = payload.save("abc123", [Upload(filename="neu.jpg", content_type="image/jpeg", data=b"neu")])

    restored = payload.load("abc123", meta)

    assert [u.data for u in restored] == [b"neu"]
    assert len(list((payload_dir / "abc123").iterdir())) == 1


def test_delete_is_idempotent(payload_dir):
    payload.save("abc123", _uploads())

    payload.delete("abc123")
    payload.delete("abc123")

    assert not (payload_dir / "abc123").exists()


def test_delete_without_any_directory_is_harmless(payload_dir):
    payload.delete("gibtesnicht")


def test_load_without_a_description_returns_none(payload_dir):
    assert payload.load("abc123", None) is None
    assert payload.load("abc123", "") is None


def test_load_returns_none_when_a_file_is_gone(payload_dir):
    meta = payload.save("abc123", _uploads())
    (payload_dir / "abc123" / "01.bin").unlink()

    assert payload.load("abc123", meta) is None


def test_load_returns_none_for_an_unreadable_description(payload_dir):
    payload.save("abc123", _uploads())

    assert payload.load("abc123", "kein json") is None
    assert payload.load("abc123", json.dumps({"nix": []})) is None


def test_total_bytes_counts_everything_kept(payload_dir):
    assert payload.total_bytes() == 0

    payload.save("abc123", _uploads())
    payload.save("def456", _uploads())

    expected = 2 * sum(len(u.data) for u in _uploads())
    assert payload.total_bytes() == expected


def test_budget_rejects_what_does_not_fit(payload_dir, monkeypatch):
    # 1 MB Budget, ein Upload von 600 KB passt, zwei nicht mehr.
    monkeypatch.setattr(config, "QUEUE_PAYLOAD_MAX_MB", 1.0)
    big = [Upload(filename="gross.jpg", content_type="image/jpeg", data=b"x" * 600_000)]

    assert payload.fits_in_budget(big) is True
    payload.save("erst", big)
    assert payload.fits_in_budget(big) is False
    # Der bereits aufbewahrte Inhalt bleibt unangetastet.
    assert payload.total_bytes() == 600_000


def test_sweep_removes_orphans_and_keeps_the_rest(payload_dir):
    payload.save("bleibt", _uploads())
    payload.save("verwaist", _uploads())

    removed = payload.sweep({"bleibt"})

    assert removed == ["verwaist"]
    assert (payload_dir / "bleibt").is_dir()
    assert not (payload_dir / "verwaist").exists()


def test_sweep_without_any_directory_is_harmless(payload_dir):
    assert payload.sweep(set()) == []
