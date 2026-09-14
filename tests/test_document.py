"""Tests 11 und 13 aus DESIGN.md §12: `document.read` liest das PDF, reicht ein JPEG
durch und weist HEIC sowie ein PDF ohne Textebene zurück; `content_hash` ist stabil
gegenüber demselben Inhalt und empfindlich gegenüber einem veränderten.

Ohne Netzzugriff: die Fixtures liegen im Repo, das Bildmodell wird hier gar nicht
aufgerufen - `document.py` liest nur (DESIGN.md §6).
"""
from __future__ import annotations

import pytest

from conftest import FIXTURES_DIR
from sources import document

PDF_BYTES = (FIXTURES_DIR / "document_kartoffelsuppe.pdf").read_bytes()
SCAN_BYTES = (FIXTURES_DIR / "document_scan_ohne_textebene.pdf").read_bytes()
JPEG_BYTES = (FIXTURES_DIR / "document_rezeptfoto.jpg").read_bytes()


def _upload(name: str, content_type: str, data: bytes) -> document.Upload:
    return document.Upload(filename=name, content_type=content_type, data=data)


def test_read_pdf_with_text_layer():
    result = document.read([_upload("rezept.pdf", "application/pdf", PDF_BYTES)])

    assert result.stage == "pdf"
    assert result.images == ()
    assert result.recipe is None
    assert "Kartoffelsuppe" in result.text
    assert "Majoran" in result.text


def test_read_pdf_respects_max_pages(monkeypatch):
    """MAX_PDF_PAGES wird durchgesetzt, bevor gelesen wird (Auftragsdetails A12, Punkt 2).
    Bei null erlaubten Seiten bleibt kein Text übrig, und das ist derselbe Fall wie ein
    gescanntes PDF."""
    monkeypatch.setattr(document.config, "MAX_PDF_PAGES", 0)

    with pytest.raises(document.UnreadablePdfError):
        document.read([_upload("rezept.pdf", "application/pdf", PDF_BYTES)])


def test_scanned_pdf_is_rejected_with_spec_message():
    with pytest.raises(document.UnreadablePdfError) as exc_info:
        document.read([_upload("scan.pdf", "application/pdf", SCAN_BYTES)])

    assert str(exc_info.value) == (
        "Dieses PDF enthält keinen lesbaren Text. Ein Foto der Seite funktioniert besser."
    )


def test_read_passes_jpeg_through_unchanged():
    result = document.read([_upload("foto.jpg", "image/jpeg", JPEG_BYTES)])

    assert result.stage == "images"
    assert result.text is None
    assert result.images == (JPEG_BYTES,)


def test_read_groups_multiple_images_into_one_result():
    """Vorder- und Rückseite einer Seite gehören zu einem Rezept (DESIGN.md §6)."""
    result = document.read([
        _upload("vorne.jpg", "image/jpeg", JPEG_BYTES),
        _upload("hinten.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"x" * 20),
    ])

    assert result.stage == "images"
    assert len(result.images) == 2


def test_heic_is_rejected_and_names_the_way_out():
    with pytest.raises(document.UnsupportedFileError) as exc_info:
        document.read([_upload("IMG_0042.HEIC", "image/heic", b"\x00" * 32)])

    message = str(exc_info.value)
    assert "HEIC" in message
    assert "JPEG" in message


def test_heic_without_content_type_is_still_rejected():
    """Das Teilen-Menü schickt Dateien gelegentlich als application/octet-stream; die
    Endung entscheidet dann."""
    with pytest.raises(document.UnsupportedFileError):
        document.read([_upload("IMG_0042.heic", "application/octet-stream", b"\x00" * 32)])


def test_other_content_types_are_rejected():
    with pytest.raises(document.UnsupportedFileError):
        document.read([_upload("notizen.txt", "text/plain", b"500 g Mehl")])


def test_mixed_pdf_and_image_is_rejected():
    with pytest.raises(document.UnsupportedFileError):
        document.read([
            _upload("rezept.pdf", "application/pdf", PDF_BYTES),
            _upload("foto.jpg", "image/jpeg", JPEG_BYTES),
        ])


def test_content_hash_is_stable_and_sensitive():
    same_bytes_other_name = [_upload("anders benannt.jpg", "image/jpeg", JPEG_BYTES)]
    original = [_upload("foto.jpg", "image/jpeg", JPEG_BYTES)]
    changed = [_upload("foto.jpg", "image/jpeg", JPEG_BYTES + b"\x00")]

    assert document.content_hash(original) == document.content_hash(same_bytes_other_name)
    assert document.content_hash(original) != document.content_hash(changed)
    assert len(document.content_hash(original)) == 16
