"""Rezeptquelle fuer YouTube-Videos.

Zwei Quellen, absichtlich getrennt:

1. Die Untertitel kommen vom Transkript-Endpunkt, den `youtube-transcript-api`
   anspricht. Der Weg ueber `yt-dlp --write-auto-sub` ist verschlossen: yt-dlp loest
   Untertitel zu einer gezeichneten `api/timedtext`-Adresse auf, und YouTube beantwortet
   die ohne Herkunftsnachweis (PO-Token) mit `HTTP Error 429`. Am 2026-10-22 live gegen
   den Dienst gemessen: die Abweisung kommt bei jedem Untertitelformat, bei jeder
   Wiederholung und bei fuenf verschiedenen Player-Clients, und sie stand noch drei Tage
   nach dem ersten gescheiterten Import. Derselbe Rechner bekommt die Untertitel vom
   Transkript-Endpunkt ohne Token, ohne Cookie und ohne Konto.
2. Titel und Videobeschreibung kommen weiter von `yt-dlp` (`--skip-download`). Dieser
   Weg ist unbeschaedigt, und die Beschreibung enthaelt oft das ausgeschriebene Rezept.

Beide scheitern unabhaengig voneinander: ohne Metadaten laeuft der Import mit dem
Transkript allein weiter, denn das Transkript ist der Rohtext, auf den es ankommt.

Dieses Modul liest nur und ruft weder Mealie noch das LLM auf. Es laedt **niemals** das
Video oder die Tonspur, und es gibt bewusst keinen Whisper-Fallback (DESIGN.md
Abschnitt 1, Nicht-Ziele Phase 1): fehlen Untertitel, wird `NoTranscriptError` geworfen.
"""
from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Iterable

import requests
from youtube_transcript_api import (
    AgeRestricted,
    FetchedTranscriptSnippet,
    InvalidVideoId,
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeTranscriptApi,
    YouTubeTranscriptApiException,
)

import classify
from sources import SourceError, SourceResult

log = logging.getLogger(__name__)

# yt-dlp muss auf dem Image liegen (requirements.txt); es wird als
# Kommandozeilenwerkzeug aufgerufen, wie in DESIGN.md Abschnitt 6 vorgegeben,
# statt ueber die (privatere, seltener stabile) Python-API.
YT_DLP_TIMEOUT = 300  # Sekunden Prozesslimit; kein SPEC-Wert, defensive Vorgabe gegen haengende Aufrufe

# Zeitlimit je HTTP-Aufruf des Transkript-Endpunkts. `requests` hat ohne Angabe gar
# keines, und die Bibliothek gibt selbst keines mit - ein haengender Aufruf wuerde die
# Importstufe unbegrenzt blockieren. Derselbe Gedanke wie bei YT_DLP_TIMEOUT.
TRANSCRIPT_TIMEOUT = 30

# Reihenfolge = Praeferenz (DESIGN.md Abschnitt 6). Ein Aufruf genuegt dafuer: die
# Bibliothek waehlt daraus die erste vorhandene Sprache und bevorzugt dabei
# geschriebene vor automatisch erzeugten Untertiteln. Der frueher noetige zweite Aufruf
# fuer Englisch - und mit ihm der Befund A9, dass ein HTTP 429 der zweiten Sprache die
# schon geladene Datei der ersten verwarf - entfaellt damit.
_TRANSCRIPT_LANGS = ("de", "en")

# Antworten *ueber dieses Video*: daran aendert ein spaeterer Versuch nichts, der Import
# scheitert endgueltig (DESIGN.md Abschnitt 7, "Kein Untertitel"). Alles andere, was die
# Bibliothek wirft, ist eine Abweisung oder eine veraenderte Antwortform und damit
# vorlaeufig - siehe ThrottledError.
_CONTENT_ERRORS = (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    InvalidVideoId,
    AgeRestricted,
)


class NoTranscriptError(SourceError):
    """Das Video hat weder manuelle noch automatische Untertitel in de/en."""


class ThrottledError(SourceError):
    """YouTube gibt die Untertitel dieses Videos gerade nicht heraus.

    Der Unterschied zu `NoTranscriptError` haengt nicht am Wortlaut einer einzelnen
    Fehlermeldung: alles, was sich nicht auf den Inhalt des Videos zurueckfuehren laesst
    (Drosselung, Botpruefung, verlangter Herkunftsnachweis, veraenderte Antwortform,
    Netzfehler), gilt als Drosselung und wird von `app.py` geparkt statt verworfen.

    Befund vom 2026-10-20: ein auf `HTTP Error 429` gemuenzter Textvergleich erkannte die
    Antwort "Sign in to confirm you're not a bot" nicht und meldete sie als "keine
    Untertitel" - einem Video mit Untertiteln in ueber hundert Sprachen. Deshalb ist die
    Zuordnung heute umgekehrt: endgueltig ist nur, was YouTube ueber das Video selbst
    sagt."""


class _TimeoutSession(requests.Session):
    """`requests.Session` mit Zeitlimit. Die Bibliothek nimmt eine Sitzung von aussen,
    gibt aber bei ihren Aufrufen kein `timeout` mit; `setdefault` laesst ein von ihr
    doch gesetztes unberuehrt."""

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", TRANSCRIPT_TIMEOUT)
        return super().request(*args, **kwargs)


def _snippets_to_text(snippets: Iterable[FetchedTranscriptSnippet]) -> str:
    """Transkriptschnipsel auf Fliesstext reduzieren. Automatisch erzeugte Untertitel
    wiederholen dieselbe Zeile oft ueber mehrere rollende Bloecke - Dubletten direkt
    hintereinander werden entfernt. Zeilenumbrueche innerhalb eines Schnipsels werden zu
    Leerzeichen, damit der Rohtext eine Zeile Fliesstext bleibt."""
    lines: list[str] = []
    last = None
    for snippet in snippets:
        line = " ".join(snippet.text.split())
        if line and line != last:
            lines.append(line)
            last = line
    return " ".join(lines)


def _fetch_transcript(video_id: str, url: str) -> str:
    """Untertitel als Fliesstext, oder ein Fehler, der sagt warum (siehe
    `_CONTENT_ERRORS` und `ThrottledError`)."""
    api = YouTubeTranscriptApi(http_client=_TimeoutSession())
    log.info("youtube: hole Untertitel (%s) fuer %s", "/".join(_TRANSCRIPT_LANGS), url)
    try:
        fetched = api.fetch(video_id, languages=_TRANSCRIPT_LANGS)
    except _CONTENT_ERRORS as exc:
        log.warning("youtube: keine Untertitel fuer %s: %s", url, type(exc).__name__)
        raise NoTranscriptError(f"Keine Untertitel (de/en) fuer {url}") from exc
    except (YouTubeTranscriptApiException, requests.RequestException) as exc:
        log.warning(
            "youtube: Untertitel fuer %s abgewiesen (%s): %s",
            url, type(exc).__name__, str(exc).strip().splitlines()[0] if str(exc).strip() else "ohne Text",
        )
        raise ThrottledError(
            f"YouTube gibt die Untertitel fuer {url} gerade nicht heraus ({type(exc).__name__})"
        ) from exc

    log.info(
        "youtube: Transkript fuer %s gelesen (%s, %s, %d Schnipsel)",
        url, fetched.language_code, "automatisch" if fetched.is_generated else "geschrieben",
        len(fetched.snippets),
    )
    return _snippets_to_text(fetched.snippets)


def _run_yt_dlp(url: str) -> dict:
    """Titel und Videobeschreibung, oder ein leeres Mapping. Ein Fehlschlag hier ist
    kein Fehlschlag des Imports: er wird gemeldet und der Import laeuft mit dem
    Transkript allein weiter. Nichts wird geschrieben - `--skip-download` und kein
    Ausgabemuster, also braucht der Aufruf auch kein Arbeitsverzeichnis mehr."""
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--print-json",
        "--no-warnings",
        url,
    ]
    log.info("yt-dlp: hole Metadaten fuer %s", url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=YT_DLP_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.warning("yt-dlp hat das Zeitlimit ueberschritten fuer %s", url)
        return {}

    if proc.returncode != 0:
        stderr = proc.stderr or ""
        stderr_tail = stderr.strip().splitlines()[-1] if stderr.strip() else "kein stderr"
        log.warning("yt-dlp fehlgeschlagen fuer %s: %s", url, stderr_tail)
        return {}

    stdout = proc.stdout.strip()
    if not stdout:
        log.warning("yt-dlp hat keine Metadaten ausgegeben fuer %s", url)
        return {}

    # --print-json gibt genau ein JSON-Objekt pro Video aus; bei Playlists waeren es
    # mehrere Zeilen, das ist hier nicht vorgesehen (Einzelvideo-Import).
    last_line = stdout.splitlines()[-1]
    try:
        info = json.loads(last_line)
    except json.JSONDecodeError:
        log.warning("yt-dlp-Ausgabe fuer %s liess sich nicht als JSON lesen", url)
        return {}
    return info if isinstance(info, dict) else {}


def fetch(url: str) -> SourceResult:
    video_id = classify.youtube_video_id(url)
    if video_id is None:
        # Kanal- oder Playlist-Link: classify() erkennt die Domain als "youtube", ein
        # Video steckt aber nicht darin (siehe classify.normalize_url).
        raise SourceError(f"In dieser YouTube-Adresse steckt kein Video: {url}")

    # Zuerst die Untertitel, danach die Metadaten: scheitert die Quelle, die ueber
    # Erfolg und Misserfolg entscheidet, wird der zweite Aufruf gar nicht mehr faellig.
    transcript = _fetch_transcript(video_id, url)
    if not transcript.strip():
        raise NoTranscriptError(f"Untertitel ohne verwertbaren Text fuer {url}")

    info = _run_yt_dlp(url)
    title = info.get("title")
    description = info.get("description") or ""

    parts = [p for p in (title, description, transcript) if p and p.strip()]
    text = "\n\n".join(parts)

    log.info("youtube: Rohtext fuer %s steht (%d Zeichen)", url, len(text))
    return SourceResult(recipe=None, text=text, title=title, stage="transcript")
