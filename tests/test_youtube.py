"""Tests für sources/youtube.py an aufgezeichneten Untertitel-Fixtures (DESIGN.md §12,
vierte Fixture). `subprocess.run` ersetzt den `yt-dlp`-Aufruf, kein Netzzugriff.

Zwei Formate, weil der Dienst ohne `ffmpeg` auskommt und deshalb kein
`--convert-subs srt` mehr aufruft: WebVTT ist das Format, das YouTube tatsächlich
liefert, SRT bleibt als früher aufgezeichnete Form gültig. Die VTT-Fixture ist aus der
SRT-Aufzeichnung desselben Videos erzeugt, samt WebVTT-Kopf, Cue-Einstellungen und den
für Auto-Untertitel typischen Wort-Zeitmarken.

Die Fixture `youtube_maultaschen.de.srt` ist ein echtes automatisch erzeugtes deutsches
Untertranskript eines Kochvideos - inklusive der für Auto-Untertitel typischen rollenden
Dubletten, an denen `_srt_to_text` seine Entdopplung beweisen muss.

Seit A15 (2026-08-23) fragt `fetch()` die Sprachen einzeln ab, weil `yt-dlp --sub-lang
de,en` in einem Aufruf bei einem HTTP-429 fuer die zweite Sprache die gesamte Extraktion
abbricht und dabei die schon geladene Datei der ersten Sprache verwirft (Befund A9). Die
Tests dazu simulieren `subprocess.run` als Funktion, die pro Aufruf gezaehlt und
unterschiedlich beantwortet werden kann.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import app as app_module
from sources import SourceError
from sources.youtube import NoTranscriptError, ThrottledError, fetch, subprocess

FIXTURES = Path(__file__).parent / "fixtures"
SRT_FIXTURE = FIXTURES / "youtube_maultaschen.de.srt"
VTT_FIXTURE = FIXTURES / "youtube_maultaschen.de.vtt"
VIDEO_ID = "w8TmZzFExlg"
TITLE = "Deutsch kochen für Anfänger | Maultaschen"


class _FakeCompletedProcess:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _info_line(
    video_id: str = VIDEO_ID, title: str = TITLE, description: str = "Ein Rezept aus der Klinikküche."
) -> str:
    return json.dumps({"id": video_id, "title": title, "description": description}) + "\n"


def test_fetch_reads_transcript_from_recorded_srt(monkeypatch):
    calls = []

    def fake_run(cmd, cwd, capture_output, text, timeout):
        calls.append(cmd)
        # yt-dlp legt die Untertiteldatei im Arbeitsverzeichnis ab, bevor fetch() sie liest.
        shutil.copy(SRT_FIXTURE, Path(cwd) / f"{VIDEO_ID}.de.srt")
        return _FakeCompletedProcess(stdout=_info_line())

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = fetch(f"https://www.youtube.com/watch?v={VIDEO_ID}")

    assert result.stage == "transcript"
    assert result.recipe is None
    assert result.title == TITLE
    assert "Maultaschen" in result.text
    assert "Heidelberg" in result.text
    # Rollende Auto-Untertitel-Dubletten müssen entfernt sein (siehe _srt_to_text).
    assert result.text.count("Im St. Elisabeth Klinikum in Heidelberg.") == 1
    # Deutsch hat sofort eine Datei ergeben: kein zweiter Aufruf fuer Englisch (DESIGN.md
    # §6 - "das spart auch den Aufruf, der die Drosselung überhaupt auslöst").
    assert len(calls) == 1
    assert calls[0][calls[0].index("--sub-lang") + 1] == "de"


def test_fetch_reads_transcript_from_recorded_vtt(monkeypatch):
    def fake_run(cmd, cwd, capture_output, text, timeout):
        # Ohne --convert-subs legt yt-dlp die Datei so ab, wie YouTube sie liefert: .vtt.
        assert "--convert-subs" not in cmd, "kein ffmpeg im Image, siehe DESIGN.md §2"
        shutil.copy(VTT_FIXTURE, Path(cwd) / f"{VIDEO_ID}.de.vtt")
        return _FakeCompletedProcess(stdout=_info_line())

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = fetch(f"https://www.youtube.com/watch?v={VIDEO_ID}")

    assert result.stage == "transcript"
    assert result.recipe is None
    assert "Maultaschen" in result.text
    assert "Heidelberg" in result.text
    # WebVTT-Kopf und Cue-Einstellungen dürfen nicht im Transkript landen.
    assert "WEBVTT" not in result.text
    assert "align:start" not in result.text
    # Wort-Zeitmarken (<00:00:26.390><c> Wort</c>) sind entfernt, das Wort bleibt.
    assert "<c>" not in result.text
    assert "00:00:26.390" not in result.text
    assert result.text.count("Im St. Elisabeth Klinikum in Heidelberg.") == 1


def test_fetch_raises_no_transcript_error_without_subtitles(monkeypatch):
    calls = []

    def fake_run(cmd, cwd, capture_output, text, timeout):
        calls.append(cmd)
        # Kein Untertitel wird ins Arbeitsverzeichnis geschrieben, fuer keine der
        # beiden Sprachen.
        info = {"id": "ohneUntertitel", "title": "Ohne Untertitel", "description": ""}
        return _FakeCompletedProcess(stdout=json.dumps(info) + "\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(NoTranscriptError):
        fetch("https://www.youtube.com/watch?v=ohneUntertitel")

    # Beide Sprachen wurden einzeln versucht, bevor aufgegeben wurde.
    assert len(calls) == 2
    langs = [cmd[cmd.index("--sub-lang") + 1] for cmd in calls]
    assert langs == ["de", "en"]


def test_fetch_falls_back_to_english_after_german_throttled(monkeypatch):
    """Kernszenario von A15: der erste Aufruf (Deutsch) liefert HTTP 429 und keine
    Datei, der zweite (Englisch) gelingt. Das Video darf trotzdem durchlaufen."""
    calls = []

    def fake_run(cmd, cwd, capture_output, text, timeout):
        lang = cmd[cmd.index("--sub-lang") + 1]
        calls.append(lang)
        if lang == "de":
            return _FakeCompletedProcess(
                returncode=1,
                stderr="ERROR: unable to download video subtitles: HTTP Error 429: Too Many Requests",
            )
        shutil.copy(VTT_FIXTURE, Path(cwd) / f"{VIDEO_ID}.en.vtt")
        return _FakeCompletedProcess(stdout=_info_line())

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = fetch(f"https://www.youtube.com/watch?v={VIDEO_ID}")

    assert calls == ["de", "en"]
    assert result.stage == "transcript"
    assert "Maultaschen" in result.text


def test_fetch_keeps_already_downloaded_file_despite_later_failure(monkeypatch):
    """DESIGN.md §6, Punkt 2: eine schon heruntergeladene Datei darf nicht verloren
    gehen, nur weil derselbe Aufruf danach (z.B. beim Ausgeben der Metadaten)
    trotzdem als Fehlschlag zurueckkommt."""

    def fake_run(cmd, cwd, capture_output, text, timeout):
        shutil.copy(SRT_FIXTURE, Path(cwd) / f"{VIDEO_ID}.de.srt")
        return _FakeCompletedProcess(
            returncode=1,
            stderr="ERROR: HTTP Error 429: Too Many Requests",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = fetch(f"https://www.youtube.com/watch?v={VIDEO_ID}")

    assert result.stage == "transcript"
    assert "Heidelberg" in result.text


def test_fetch_raises_throttled_error_when_both_languages_are_rate_limited(monkeypatch):
    def fake_run(cmd, cwd, capture_output, text, timeout):
        return _FakeCompletedProcess(
            returncode=1,
            stderr="ERROR: unable to download video subtitles: HTTP Error 429: Too Many Requests",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ThrottledError):
        fetch(f"https://www.youtube.com/watch?v={VIDEO_ID}")


def test_throttled_error_is_a_source_error():
    # app.py unterscheidet Fehlerarten am Klassennamen (DESIGN.md §7) - ThrottledError
    # muss deshalb eine SourceError-Unterklasse bleiben.
    assert issubclass(ThrottledError, SourceError)


def test_throttled_error_maps_to_the_spec_wording():
    # app.py._describe_source_error muss den wörtlichen Text aus DESIGN.md §7 liefern,
    # nicht "kein Rezept gefunden" (A15).
    exc = ThrottledError("YouTube drosselt gerade die Untertitel fuer https://x")
    assert app_module._describe_source_error(exc) == (
        "YouTube drosselt gerade die Untertitel. Bitte später erneut teilen."
    )


def test_no_transcript_error_is_a_source_error():
    # app.py unterscheidet Fehlerarten am Klassennamen (DESIGN.md §7) - NoTranscriptError
    # muss deshalb eine SourceError-Unterklasse bleiben.
    assert issubclass(NoTranscriptError, SourceError)
