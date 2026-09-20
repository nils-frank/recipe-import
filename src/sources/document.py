"""Rezeptquelle fuer hochgeladene Dateien (Foto einer Rezeptseite, Screenshot, PDF),
siehe DESIGN.md §6 (`sources/document.py`, A12) und recipe-import-plan.md,
Auftragsdetails A12.

Wie die uebrigen Quellen liest dieses Modul nur: es ruft weder Mealie noch das LLM auf.
Ein PDF wird zu Rohtext, ein Bild geht unveraendert als Bytes weiter - die Bilderkennung
selbst macht das Bildmodell in llm.py.

Bewusst nicht enthalten:

- **Kein OCR und keine Seitenrasterung.** Ein PDF ohne Textebene ist ein gescanntes
  Dokument; OCR im Dienst hiesse Tesseract plus Sprachdaten im Image (DESIGN.md §2: ein
  Container, keine Systemwerkzeuge nachtraeglich). Ein Foto derselben Seite geht denselben
  Weg ueber das Bildmodell und liefert das bessere Ergebnis.
- **Keine Bildbearbeitung.** Kein Skalieren, kein Umkodieren. Die Bytes, die das Geraet
  geschickt hat, sind die Bytes, die das Modell sieht.
- **Kein HEIC.** Eine HEIC-Umwandlung braucht eine weitere Systembibliothek im Image,
  und das Geraet, das das Foto gemacht hat, kann sie verlustfrei selbst. Der Kurzbefehl
  wandelt vorher nach JPEG (DESIGN.md §9). Die Ablehnung nennt deshalb den Ausweg statt nur
  den Typ.
"""
from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.errors import PdfReadError

import config
from sources import SourceError, SourceResult

log = logging.getLogger(__name__)

PDF_CONTENT_TYPE = "application/pdf"
IMAGE_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

# Der Kurzbefehl soll HEIC nach JPEG wandeln (DESIGN.md §9). Kommt trotzdem HEIC an, ist
# das kein unbekannter Typ, sondern ein bekannter mit bekanntem Ausweg - deshalb eine
# eigene Liste und eine eigene Meldung.
HEIC_CONTENT_TYPES = frozenset({"image/heic", "image/heif", "image/heic-sequence"})

_SUFFIX_TO_TYPE = {
    ".pdf": PDF_CONTENT_TYPE,
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}

# Weniger als das aus einem PDF gelesen heisst: da ist keine Textebene, nur Bildseiten.
# Ein echtes Rezept auf einer Seite liegt deutlich darueber, eine reine Kopf- und
# Fusszeile deutlich darunter.
_MIN_PDF_CHARS = 200


class UnsupportedFileError(SourceError):
    """Dateityp, den dieser Dienst nicht liest. Der Text nennt den Ausweg."""


class UnreadablePdfError(SourceError):
    """PDF ohne Textebene - gescannt. Entspricht der Rueckmeldung "Gescanntes PDF"
    aus DESIGN.md §7."""


@dataclass(frozen=True)
class Upload:
    filename: str
    content_type: str
    data: bytes


def content_hash(uploads: list[Upload]) -> str:
    """sha256 ueber alle Bytes, hex, 16 Zeichen - derselbe Hash-Raum wie `url_hash`.

    Nur die Bytes gehen ein, nicht der Dateiname: dieselbe Datei zweimal geteilt ist
    derselbe Import, auch wenn das Teilen-Menue sie beim zweiten Mal anders benennt
    (DESIGN.md §12, Punkt 13).
    """
    digest = hashlib.sha256()
    for upload in uploads:
        digest.update(upload.data)
    return digest.hexdigest()[:16]


def _kind(upload: Upload) -> str:
    """Normalisierter Inhaltstyp. Faellt auf die Dateiendung zurueck, weil das
    Teilen-Menue Dateien gelegentlich als `application/octet-stream` schickt."""
    declared = (upload.content_type or "").split(";")[0].strip().lower()
    if declared and declared != "application/octet-stream":
        return declared
    name = (upload.filename or "").lower()
    for suffix, content_type in _SUFFIX_TO_TYPE.items():
        if name.endswith(suffix):
            return content_type
    return declared or "unbekannt"


def _read_pdf(upload: Upload) -> SourceResult:
    try:
        reader = PdfReader(io.BytesIO(upload.data))
        if reader.is_encrypted:
            raise UnsupportedFileError(
                f"{upload.filename} ist passwortgeschuetzt und laesst sich nicht lesen."
            )
        pages = reader.pages[: config.MAX_PDF_PAGES]
        text = "\n\n".join((page.extract_text() or "") for page in pages).strip()
    except UnsupportedFileError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        raise UnsupportedFileError(f"{upload.filename} ist kein lesbares PDF: {exc}") from exc

    if len(text) < _MIN_PDF_CHARS:
        raise UnreadablePdfError(
            "Dieses PDF enthält keinen lesbaren Text. Ein Foto der Seite funktioniert besser."
        )

    log.info("document: %d Zeichen aus %s gelesen (%d Seiten)", len(text), upload.filename, len(pages))
    return SourceResult(recipe=None, text=text, title=upload.filename, stage="pdf")


def read(uploads: list[Upload]) -> SourceResult:
    """Liest die hochgeladenen Dateien einer Anfrage zu genau einem Rezept.

    Mehrere Bilder derselben Anfrage gehoeren zusammen (Vorder- und Rueckseite einer
    Seite) und gehen gemeinsam in einen einzigen LLM-Aufruf. Ein PDF steht dagegen fuer
    sich: mehrere PDFs in einer Anfrage waeren mehrere Rezepte, und die stillschweigend
    auf das erste zu reduzieren waere schlimmer als eine Meldung.
    """
    if not uploads:
        raise UnsupportedFileError("Es war keine Datei dabei.")

    # `kinds` entsteht Element für Element aus `uploads`, die Längen können also nicht
    # auseinanderlaufen. Deshalb steht unten überall `strict=True`: ein Unterschied
    # wäre ein Fehler in dieser Funktion und soll auffallen, nicht stumm kürzen.
    kinds = [_kind(u) for u in uploads]

    heic = [u.filename for u, k in zip(uploads, kinds, strict=True) if k in HEIC_CONTENT_TYPES]
    if heic:
        raise UnsupportedFileError(
            f"{heic[0]} ist ein HEIC-Bild. Bild im Kurzbefehl nach JPEG umwandeln, "
            "dann klappt es."
        )

    unknown = [(u.filename, k) for u, k in zip(uploads, kinds, strict=True)
               if k != PDF_CONTENT_TYPE and k not in IMAGE_CONTENT_TYPES]
    if unknown:
        name, kind = unknown[0]
        raise UnsupportedFileError(
            f"{name} ist vom Typ {kind}. Ich lese PDF, JPEG, PNG und WebP."
        )

    pdfs = [u for u, k in zip(uploads, kinds, strict=True) if k == PDF_CONTENT_TYPE]
    images = [u for u, k in zip(uploads, kinds, strict=True) if k in IMAGE_CONTENT_TYPES]

    if pdfs and images:
        raise UnsupportedFileError("PDF und Bilder gemischt. Bitte getrennt teilen.")
    if len(pdfs) > 1:
        raise UnsupportedFileError("Mehrere PDFs auf einmal. Bitte eins nach dem anderen teilen.")

    if pdfs:
        return _read_pdf(pdfs[0])

    empty = [u.filename for u in images if not u.data]
    if empty:
        raise UnsupportedFileError(f"{empty[0]} ist leer.")

    log.info("document: %d Bild(er) unveraendert weitergereicht", len(images))
    return SourceResult(
        recipe=None,
        text=None,
        title=images[0].filename,
        stage="images",
        images=tuple(u.data for u in images),
    )
