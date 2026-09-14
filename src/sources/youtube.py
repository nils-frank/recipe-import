"""Rezeptquelle fuer YouTube-Videos.

`yt-dlp` liefert Titel, Videobeschreibung und - falls vorhanden - Untertitel,
ohne dass je Video oder Tonspur geladen werden (`--skip-download`). Beides
zusammen ergibt den Rohtext fuer die LLM-Stufe (llm.py, A2); dieses Modul
selbst liest nur und ruft weder Mealie noch das LLM auf.

Untertitel werden in dem Format gelesen, in dem YouTube sie liefert (WebVTT),
statt sie von `yt-dlp` nach SRT umwandeln zu lassen: `--convert-subs` ruft
`ffmpeg` auf, das im Basis-Image nicht liegt und nur fuer diese reine
Textumwandlung rund 400 MB auf eine SD-Karte mit 11,9 GB frei legen wuerde.
Der Parser unten liest beide Formate, SRT bleibt also gueltig.

Es gibt bewusst keinen Whisper-Fallback (DESIGN.md Abschnitt 1, Nicht-Ziele
Phase 1): fehlen Untertitel, wird `NoTranscriptError` geworfen statt auf
einen Download auszuweichen.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from sources import SourceError, SourceResult

log = logging.getLogger(__name__)

# yt-dlp muss auf dem Image liegen (requirements.txt); es wird als
# Kommandozeilenwerkzeug aufgerufen, wie in DESIGN.md Abschnitt 6 vorgegeben,
# statt ueber die (privatere, seltener stabile) Python-API.
YT_DLP_TIMEOUT = 300  # Sekunden Prozesslimit; kein SPEC-Wert, defensive Vorgabe gegen haengende Aufrufe

_SUB_LANGS = ("de", "en")  # Reihenfolge = Praeferenz, siehe DESIGN.md Abschnitt 6

# Untertitelformate: SRT nummeriert seine Bloecke und trennt Millisekunden mit Komma,
# WebVTT nutzt Punkt und haengt Cue-Einstellungen ("align:start position:0%") an.
_SUB_EXTENSIONS = ("vtt", "srt")  # Reihenfolge = Praeferenz; YouTube liefert vtt
_SUB_INDEX_RE = re.compile(r"^\d+$")
_SUB_TIMESTAMP_RE = re.compile(r"^(?:\d{2}:)?\d{2}:\d{2}[,.]\d{3}\s*-->\s*(?:\d{2}:)?\d{2}:\d{2}[,.]\d{3}")
_SUB_HEADER_RE = re.compile(r"^(WEBVTT|Kind:|Language:|NOTE|STYLE|REGION)\b")
_SUB_TAG_RE = re.compile(r"<[^>]+>")

# yt-dlp meldet eine Drosselung als "HTTP Error 429" in stderr - live gesehen von A9 am
# 2026-08-23, siehe Klassen-Docstring von ThrottledError.
_THROTTLE_RE = re.compile(r"HTTP Error 429")


class NoTranscriptError(SourceError):
    """Das Video hat weder manuelle noch automatische Untertitel in de/en."""


class ThrottledError(SourceError):
    """YouTube hat die Untertitel-Anfrage gedrosselt (HTTP 429).

    Befund A9 am 2026-08-23: `yt-dlp --sub-lang de,en` in einem Aufruf laedt
    `<id>.de.vtt` erfolgreich und bricht danach am HTTP-429 fuer `en` die gesamte
    Extraktion ab, samt Verwerfen der schon geladenen Datei. Deshalb (A15, DESIGN.md
    Abschnitt 6) genau eine Sprache je Aufruf, und eine Drosselung wird als solche
    gemeldet statt als "keine Untertitel" (DESIGN.md Abschnitt 7)."""


def _run_yt_dlp(url: str, work_dir: Path, lang: str) -> tuple[dict | None, bool]:
    # Live verifiziert am 2026-08-23 (Review A8, Befund 4): `yt-dlp --skip-download
    # --write-auto-sub --write-sub --sub-lang en --output "%(id)s.%(ext)s"` gegen ein
    # echtes YouTube-Video (dQw4w9WgXcQ) legt die Untertiteldatei tatsächlich als
    # "<id>.<lang>.<ext>" ab (verbose-Log: "Writing video subtitles to:
    # dQw4w9WgXcQ.en.vtt", Datei danach vorhanden) - die Annahme in
    # `_find_subtitle_file` stimmt.
    #
    # Geaendert am 2026-08-23 (A15) nach dem Befund aus A9: genau eine Sprache je
    # Aufruf, nie `--sub-lang de,en` zusammen - yt-dlp bricht dort beim ersten
    # HTTP-429 einer Sprache die ganze Extraktion ab und verwirft die bereits
    # geladene Datei der anderen (siehe ThrottledError). Diese Funktion wirft deshalb
    # nichts mehr: sie meldet Erfolg oder Fehlschlag (und ob er eine Drosselung war)
    # an fetch() zurueck, das nach jedem Aufruf zuerst das Arbeitsverzeichnis prueft,
    # bevor es ueberhaupt einen Fehler in Erwaegung zieht.
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-auto-sub",
        "--write-sub",
        "--sub-lang", lang,
        "--print-json",
        "--no-warnings",
        "--output", "%(id)s.%(ext)s",
        url,
    ]
    log.info("yt-dlp: hole Metadaten und Untertitel (%s) fuer %s", lang, url)
    try:
        proc = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=YT_DLP_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log.warning("yt-dlp (%s) hat das Zeitlimit ueberschritten fuer %s", lang, url)
        return None, False

    if proc.returncode != 0:
        stderr = proc.stderr or ""
        stderr_tail = stderr.strip().splitlines()[-1] if stderr.strip() else "kein stderr"
        throttled = bool(_THROTTLE_RE.search(stderr))
        log.warning("yt-dlp (%s) fehlgeschlagen fuer %s: %s", lang, url, stderr_tail)
        return None, throttled

    stdout = proc.stdout.strip()
    if not stdout:
        log.warning("yt-dlp (%s) hat keine Metadaten ausgegeben fuer %s", lang, url)
        return None, False

    # --print-json gibt genau ein JSON-Objekt pro Video aus; bei Playlists waeren es
    # mehrere Zeilen, das ist hier nicht vorgesehen (Einzelvideo-Import).
    last_line = stdout.splitlines()[-1]
    try:
        return json.loads(last_line), False
    except json.JSONDecodeError:
        log.warning("yt-dlp-Ausgabe (%s) fuer %s liess sich nicht als JSON lesen", lang, url)
        return None, False


def _subtitle_to_text(content: str) -> str:
    """WebVTT oder SRT auf Fliesstext reduzieren. Auto-generierte Untertitel wiederholen
    dieselbe Zeile oft ueber mehrere rollende Bloecke - Dubletten werden entfernt.
    WebVTT setzt zusaetzlich Wort-Zeitmarken (`<00:00:26.390><c> Wort</c>`) mitten in die
    Zeile; die entfernt dieselbe Tag-Regel, die in SRT die Formatierungs-Tags wegnimmt."""
    lines: list[str] = []
    last = None
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if (
            not line
            or _SUB_INDEX_RE.match(line)
            or _SUB_TIMESTAMP_RE.match(line)
            or _SUB_HEADER_RE.match(line)
        ):
            continue
        line = _SUB_TAG_RE.sub("", line).strip()
        if line and line != last:
            lines.append(line)
            last = line
    return " ".join(lines)


def _find_subtitle_file(work_dir: Path) -> Path | None:
    # Ueber die Video-ID gesucht wurde frueher, als die Datei stets aus derselben
    # yt-dlp-Antwort kam, deren JSON die ID lieferte. Seit A15 wird pro Sprache
    # einzeln aufgerufen, und ein fehlgeschlagener Aufruf liefert kein JSON mehr -
    # die Datei kann trotzdem geschrieben worden sein (DESIGN.md Abschnitt 6, Punkt 2:
    # eine schon heruntergeladene Datei darf durch den Fehlschlag der naechsten
    # Sprache nicht verloren gehen). `work_dir` ist pro Aufruf ein frisches
    # Verzeichnis, ein Glob nach Sprache und Endung reicht deshalb unabhaengig von
    # der ID.
    for lang in _SUB_LANGS:
        for ext in _SUB_EXTENSIONS:
            matches = sorted(work_dir.glob(f"*.{lang}.{ext}"))
            if matches:
                return matches[0]
    return None


def fetch(url: str) -> SourceResult:
    work_dir = Path(tempfile.mkdtemp(prefix="recipe-import-yt-", dir="/tmp"))
    try:
        info: dict = {}
        subtitle_file: Path | None = None
        throttled = False

        # Eine Sprache je Aufruf (DESIGN.md Abschnitt 6, A15): Deutsch zuerst, Englisch
        # nur, wenn Deutsch keine Datei ergeben hat. Nach jedem Aufruf wird das
        # Arbeitsverzeichnis geprueft, bevor irgendein Fehler in Erwaegung gezogen
        # wird - so geht eine schon geladene Datei nicht verloren, falls der naechste
        # Aufruf (der dann gar nicht mehr noetig ist) scheitern wuerde.
        for lang in _SUB_LANGS:
            lang_info, lang_throttled = _run_yt_dlp(url, work_dir, lang)
            if lang_info:
                info = lang_info
            throttled = throttled or lang_throttled
            subtitle_file = _find_subtitle_file(work_dir)
            if subtitle_file is not None:
                break

        if subtitle_file is None:
            if throttled:
                raise ThrottledError(f"YouTube drosselt gerade die Untertitel fuer {url}")
            raise NoTranscriptError(f"Keine Untertitel (de/en) fuer {url}")

        title = info.get("title")
        description = info.get("description") or ""

        transcript = _subtitle_to_text(subtitle_file.read_text(encoding="utf-8", errors="replace"))
        if not transcript.strip():
            raise NoTranscriptError(f"Untertiteldatei ohne verwertbaren Text fuer {url}")

        parts = [p for p in (title, description, transcript) if p and p.strip()]
        text = "\n\n".join(parts)

        log.info(
            "youtube: Transkript fuer %s gelesen (%s, %d Zeichen)",
            url, subtitle_file.name, len(transcript),
        )
        return SourceResult(recipe=None, text=text, title=title, stage="transcript")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
