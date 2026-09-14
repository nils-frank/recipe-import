"""Gemeinsamer Vertrag der Rezeptquellen (site.py, youtube.py, document.py).

Beide Module liefern eine `SourceResult` und rufen niemals Mealie oder das
LLM auf - sie lesen nur. `SourceError` liegt hier statt in einem der beiden
Module, damit keines das andere importieren muss, um denselben Fehlertyp zu
werfen bzw. abzufangen (siehe app.py, A4, fuer die Ruckmeldungstexte je
Fehlerart in DESIGN.md Abschnitt 7).
"""
from __future__ import annotations

from dataclasses import dataclass

from schema import Recipe


class SourceError(RuntimeError):
    """Weder ein Recipe noch verwertbarer Rohtext liess sich aus der Quelle gewinnen."""


@dataclass(frozen=True)
class SourceResult:
    recipe: Recipe | None    # gefuellt, wenn ohne LLM loesbar
    text: str | None         # Rohtext fuer die LLM-Stufe, wenn recipe None ist
    title: str | None
    stage: str                # "scrapers" | "jsonld" | "text" | "transcript" | "caption" | "pdf" | "images"
    # Additiv fuer den Uploadweg (A12, DESIGN.md §6): Bildbytes fuer das Bildmodell,
    # unveraendert wie hochgeladen. Nur document.py fuellt dieses Feld; die uebrigen
    # Quellen lassen es leer, deshalb ein Vorgabewert statt einer Aenderung an ihnen.
    images: tuple[bytes, ...] = ()
