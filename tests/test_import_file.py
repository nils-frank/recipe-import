"""Test 12 aus DESIGN.md §12: `POST /import/file` antwortet `401` ohne Token, `413` über
`MAX_UPLOAD_MB` und `202` im Normalfall. Dazu der gemeinsame Ratenzähler beider
Endpunkte (DESIGN.md §11).

`import_file` wird wie in `test_ratelimit.py` direkt aufgerufen statt über einen
HTTP-Client: `fastapi.testclient` setzt `httpx` voraus, das laut DESIGN.md §2 nicht Teil
dieses Projekts ist. Die `UploadFile`-Objekte werden deshalb von Hand gebaut - genau so,
wie Starlette sie aus einem multipart-Anfragetext erzeugt.
"""
from __future__ import annotations

import asyncio
import io

import pytest
from fastapi import BackgroundTasks, HTTPException
from starlette.datastructures import Headers, UploadFile

import app as app_module
import config
from conftest import FIXTURES_DIR

JPEG_BYTES = (FIXTURES_DIR / "document_rezeptfoto.jpg").read_bytes()
GOOD_TOKEN = f"Bearer {config.IMPORT_TOKEN}"


def _upload_file(name: str, content_type: str, data: bytes) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data),
        size=len(data),
        filename=name,
        headers=Headers({"content-type": content_type}),
    )


def _call(files, authorization):
    return asyncio.run(
        app_module.import_file(BackgroundTasks(), files=files, authorization=authorization)
    )


@pytest.fixture(autouse=True)
def _no_background_work(monkeypatch):
    """Der Hintergrundweg gehört nicht zu diesem Test - er würde Mealie und das LLM
    rufen. Geprüft wird nur der HTTP-Eingang."""
    monkeypatch.setattr(app_module.store, "recent_count", lambda seconds: 0)
    monkeypatch.setattr(app_module, "_process_file", lambda uploads: None)


def test_rejects_missing_token():
    with pytest.raises(HTTPException) as exc_info:
        _call([_upload_file("foto.jpg", "image/jpeg", JPEG_BYTES)], None)

    assert exc_info.value.status_code == 401


def test_rejects_wrong_token():
    with pytest.raises(HTTPException) as exc_info:
        _call([_upload_file("foto.jpg", "image/jpeg", JPEG_BYTES)], "Bearer falsch")

    assert exc_info.value.status_code == 401


def test_rejects_upload_over_max_upload_mb(monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    too_big = b"\xff\xd8\xff" + b"\x00" * (1024 * 1024 + 1)

    with pytest.raises(HTTPException) as exc_info:
        _call([_upload_file("gross.jpg", "image/jpeg", too_big)], GOOD_TOKEN)

    assert exc_info.value.status_code == 413


def test_rejects_more_files_than_allowed(monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_FILES", 2)
    files = [_upload_file(f"foto{i}.jpg", "image/jpeg", JPEG_BYTES) for i in range(3)]

    with pytest.raises(HTTPException) as exc_info:
        _call(files, GOOD_TOKEN)

    assert exc_info.value.status_code == 400


def test_accepts_valid_upload(monkeypatch):
    seen = []
    monkeypatch.setattr(app_module, "_process_file", lambda uploads: seen.append(uploads))

    result = _call([_upload_file("foto.jpg", "image/jpeg", JPEG_BYTES)], GOOD_TOKEN)

    assert result == {"status": "accepted"}


def test_shares_the_rate_limit_with_the_url_endpoint(monkeypatch):
    """Ein Zähler für beide Wege (DESIGN.md §11): steht er am Limit, wird auch der
    Uploadweg abgewiesen."""
    monkeypatch.setattr(app_module.store, "recent_count", lambda seconds: config.RATE_LIMIT_PER_HOUR)

    with pytest.raises(HTTPException) as exc_info:
        _call([_upload_file("foto.jpg", "image/jpeg", JPEG_BYTES)], GOOD_TOKEN)

    assert exc_info.value.status_code == 429
