"""Tests für sources/youtube.py, ohne Netzzugriff (DESIGN.md §12).

Zwei Dinge werden ersetzt: der Transkript-Endpunkt (`YouTubeTranscriptApi`) und der
`yt-dlp`-Aufruf (`subprocess.run`). Beide Quellen sind seit
openspec/changes/fix-youtube-transcript-fetch getrennt - die Untertitel kommen vom
Endpunkt, Titel und Beschreibung von yt-dlp -, und die Tests prüfen genau das: jede
Quelle für sich und der Fall, dass eine von beiden ausfällt.

Die Fixture `youtube_maultaschen.de.snippets.json` ist eine echte Aufzeichnung des
Transkript-Endpunkts zum deutschen Kochvideo `w8TmZzFExlg` (101 Schnipsel, automatisch
erzeugte Untertitel, aufgezeichnet am 2026-10-22). Sie hat die Form, in der der Endpunkt
antwortet: ein Schnipsel je Einblendung, ohne die rollenden Dubletten der früheren
WebVTT-Aufzeichnung. Die Entdopplung wird deshalb an einer eigens gebauten Rolle geprüft
(`test_snippets_to_text_...`), die die Form zeigt, die ein Untertitelgleis trotzdem haben
kann.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests
from youtube_transcript_api import (
    AgeRestricted,
    FetchedTranscript,
    FetchedTranscriptSnippet,
    InvalidVideoId,
    IpBlocked,
    NoTranscriptFound,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeDataUnparsable,
    YouTubeRequestFailed,
    YouTubeTranscriptApiException,
)

import app as app_module
from sources import SourceError
from sources import youtube as youtube_module
from sources.youtube import NoTranscriptError, ThrottledError, fetch, subprocess

FIXTURES = Path(__file__).parent / "fixtures"
SNIPPETS_FIXTURE = FIXTURES / "youtube_maultaschen.de.snippets.json"
VIDEO_ID = "w8TmZzFExlg"
TITLE = "Deutsch kochen für Anfänger | Maultaschen"
DESCRIPTION = "Ein Rezept aus der Klinikküche."


class _FakeCompletedProcess:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _info_line(video_id: str = VIDEO_ID, title: str = TITLE, description: str = DESCRIPTION) -> str:
    return json.dumps({"id": video_id, "title": title, "description": description}) + "\n"


def _recorded_snippets() -> list[FetchedTranscriptSnippet]:
    raw = json.loads(SNIPPETS_FIXTURE.read_text(encoding="utf-8"))
    return [
        FetchedTranscriptSnippet(text=s["text"], start=s["start"], duration=s["duration"])
        for s in raw
    ]


def _fetched(snippets: list[FetchedTranscriptSnippet], language_code: str = "de") -> FetchedTranscript:
    return FetchedTranscript(
        snippets=snippets,
        video_id=VIDEO_ID,
        language="German (auto-generated)",
        language_code=language_code,
        is_generated=True,
    )


def _stub_transcript(monkeypatch, answer, calls: list | None = None) -> None:
    """Ersetzt den Transkript-Endpunkt. `answer` ist entweder ein `FetchedTranscript`
    oder eine Ausnahme, die der Aufruf wirft."""

    class _FakeApi:
        def __init__(self, http_client=None, proxy_config=None):
            self.http_client = http_client

        def fetch(self, video_id, languages=("en",), preserve_formatting=False):
            if calls is not None:
                calls.append((video_id, tuple(languages)))
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr(youtube_module, "YouTubeTranscriptApi", _FakeApi)


def _stub_yt_dlp(monkeypatch, process: _FakeCompletedProcess, calls: list | None = None) -> None:
    def fake_run(cmd, capture_output, text, timeout):
        if calls is not None:
            calls.append(cmd)
        return process

    monkeypatch.setattr(subprocess, "run", fake_run)


# --- Der Normalfall -----------------------------------------------------------------


def test_fetch_reads_transcript_from_recorded_snippets(monkeypatch):
    transcript_calls: list = []
    _stub_transcript(monkeypatch, _fetched(_recorded_snippets()), transcript_calls)
    _stub_yt_dlp(monkeypatch, _FakeCompletedProcess(stdout=_info_line()))

    result = fetch(f"https://www.youtube.com/watch?v={VIDEO_ID}")

    assert result.stage == "transcript"
    assert result.recipe is None
    assert result.title == TITLE
    assert "Maultaschen" in result.text
    assert "Heidelberg" in result.text
    # Titel und Beschreibung stehen vor dem Transkript im Rohtext.
    assert result.text.startswith(TITLE)
    assert DESCRIPTION in result.text
    # Deutsch zuerst, Englisch als Rückfall - in genau einem Aufruf (DESIGN.md §6).
    assert transcript_calls == [(VIDEO_ID, ("de", "en"))]


def test_fetch_passes_no_subtitle_flags_to_yt_dlp(monkeypatch):
    """yt-dlp holt nur noch Metadaten. Würde hier wieder ein Untertitelschalter
    mitgehen, liefe der Aufruf in die PO-Token-Abweisung, die diese Änderung
    umgeht."""
    calls: list = []
    _stub_transcript(monkeypatch, _fetched(_recorded_snippets()))
    _stub_yt_dlp(monkeypatch, _FakeCompletedProcess(stdout=_info_line()), calls)

    fetch(f"https://www.youtube.com/watch?v={VIDEO_ID}")

    assert len(calls) == 1
    cmd = calls[0]
    assert "--skip-download" in cmd
    assert "--print-json" in cmd
    for flag in ("--write-auto-sub", "--write-sub", "--sub-lang", "--sub-format", "--convert-subs"):
        assert flag not in cmd, f"{flag} gehört nicht mehr in den Aufruf"


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://m.youtube.com/watch?v={VIDEO_ID}&feature=share",
    ],
)
def test_fetch_finds_the_video_id_in_every_url_shape(monkeypatch, url):
    transcript_calls: list = []
    _stub_transcript(monkeypatch, _fetched(_recorded_snippets()), transcript_calls)
    _stub_yt_dlp(monkeypatch, _FakeCompletedProcess(stdout=_info_line()))

    result = fetch(url)

    assert transcript_calls[0][0] == VIDEO_ID
    assert result.stage == "transcript"


def test_fetch_rejects_a_youtube_url_without_a_video(monkeypatch):
    # Kanal- oder Playlist-Link: classify() nennt die Quelle "youtube", ein Video steckt
    # aber nicht darin - dann wird der Endpunkt gar nicht erst gefragt.
    def kein_aufruf(*args, **kwargs):
        raise AssertionError("der Transkript-Endpunkt darf hier nicht gefragt werden")

    monkeypatch.setattr(youtube_module, "YouTubeTranscriptApi", kein_aufruf)

    with pytest.raises(SourceError):
        fetch("https://www.youtube.com/@einkanal")


# --- Fliesstext aus Schnipseln -------------------------------------------------------


def test_snippets_to_text_drops_rolling_duplicates_and_newlines():
    snippets = [
        FetchedTranscriptSnippet(text="Im St. Elisabeth Klinikum in Heidelberg.", start=0.0, duration=1.0),
        FetchedTranscriptSnippet(text="Im St. Elisabeth Klinikum in Heidelberg.", start=1.0, duration=1.0),
        FetchedTranscriptSnippet(text="Medizinstudentin Vivian Deu\nin ihrem", start=2.0, duration=1.0),
        FetchedTranscriptSnippet(text="   ", start=3.0, duration=1.0),
        FetchedTranscriptSnippet(text=" Praxissemester.", start=4.0, duration=1.0),
    ]

    text = youtube_module._snippets_to_text(snippets)

    assert text.count("Im St. Elisabeth Klinikum in Heidelberg.") == 1
    assert "\n" not in text
    assert text.endswith("Medizinstudentin Vivian Deu in ihrem Praxissemester.")


def test_fetch_raises_no_transcript_error_for_an_empty_transcript(monkeypatch):
    _stub_transcript(monkeypatch, _fetched([FetchedTranscriptSnippet(text="  ", start=0.0, duration=1.0)]))
    _stub_yt_dlp(monkeypatch, _FakeCompletedProcess(stdout=_info_line()))

    with pytest.raises(NoTranscriptError):
        fetch(f"https://www.youtube.com/watch?v={VIDEO_ID}")


# --- Die Metadatenquelle fällt aus ---------------------------------------------------


def test_fetch_survives_a_failing_metadata_call(monkeypatch):
    """Ein Fehlschlag von yt-dlp ist kein Fehlschlag des Imports: das Transkript allein
    ist der Rohtext, auf den es ankommt."""
    _stub_transcript(monkeypatch, _fetched(_recorded_snippets()))
    _stub_yt_dlp(monkeypatch, _FakeCompletedProcess(returncode=1, stderr="ERROR: boom"))

    result = fetch(f"https://www.youtube.com/watch?v={VIDEO_ID}")

    assert result.stage == "transcript"
    assert result.title is None
    assert "Heidelberg" in result.text


def test_fetch_survives_unparsable_metadata(monkeypatch):
    _stub_transcript(monkeypatch, _fetched(_recorded_snippets()))
    _stub_yt_dlp(monkeypatch, _FakeCompletedProcess(stdout="kein JSON\n"))

    result = fetch(f"https://www.youtube.com/watch?v={VIDEO_ID}")

    assert result.title is None
    assert "Maultaschen" in result.text


def test_fetch_survives_a_metadata_timeout(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout):
        assert timeout == youtube_module.YT_DLP_TIMEOUT
        raise subprocess.TimeoutExpired(cmd, timeout)

    _stub_transcript(monkeypatch, _fetched(_recorded_snippets()))
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = fetch(f"https://www.youtube.com/watch?v={VIDEO_ID}")

    assert result.title is None
    assert "Heidelberg" in result.text


def test_fetch_keeps_the_transcript_when_the_description_is_empty(monkeypatch):
    _stub_transcript(monkeypatch, _fetched(_recorded_snippets()))
    _stub_yt_dlp(monkeypatch, _FakeCompletedProcess(stdout=_info_line(description="")))

    result = fetch(f"https://www.youtube.com/watch?v={VIDEO_ID}")

    assert result.title == TITLE
    assert "Heidelberg" in result.text


# --- Einordnung der Fehlschläge ------------------------------------------------------


class _UnbekannterFehler(YouTubeTranscriptApiException):
    """Eine Ausnahme, die die Bibliothek heute noch nicht wirft - sie muss trotzdem als
    Drosselung gelten, nicht als "keine Untertitel"."""


@pytest.mark.parametrize(
    "exc",
    [
        NoTranscriptFound(VIDEO_ID, ["de", "en"], None),
        TranscriptsDisabled(VIDEO_ID),
        VideoUnavailable(VIDEO_ID),
        VideoUnplayable(VIDEO_ID, "Grund", []),
        InvalidVideoId(VIDEO_ID),
        AgeRestricted(VIDEO_ID),
    ],
    ids=lambda exc: type(exc).__name__,
)
def test_an_answer_about_the_video_is_permanent(monkeypatch, exc):
    _stub_transcript(monkeypatch, exc)

    with pytest.raises(NoTranscriptError):
        fetch(f"https://www.youtube.com/watch?v={VIDEO_ID}")


@pytest.mark.parametrize(
    "exc",
    [
        RequestBlocked(VIDEO_ID),
        IpBlocked(VIDEO_ID),
        PoTokenRequired(VIDEO_ID),
        YouTubeRequestFailed(VIDEO_ID, requests.exceptions.HTTPError("HTTP Error 429")),
        YouTubeDataUnparsable(VIDEO_ID),
        _UnbekannterFehler(),
        requests.RequestException("Verbindung weg"),
    ],
    ids=lambda exc: type(exc).__name__,
)
def test_every_other_refusal_is_a_throttle(monkeypatch, exc):
    """Umgekehrte Vorgabe seit dem Befund vom 2026-10-20: endgültig ist nur, was YouTube
    über das Video sagt. Alles andere - Drosselung, Botprüfung, verlangter
    Herkunftsnachweis, veränderte Antwortform, Netzfehler - ist eine Drosselung und wird
    geparkt."""
    _stub_transcript(monkeypatch, exc)

    with pytest.raises(ThrottledError):
        fetch(f"https://www.youtube.com/watch?v={VIDEO_ID}")


def test_a_bot_check_is_queued_with_the_throttle_wording(monkeypatch):
    """Der Fall, der am 2026-10-20 live schiefging: YouTube verlangte eine Botprüfung,
    und der Mensch bekam "Das Video hat keine Untertitel" zu einem Video mit Untertiteln
    in über hundert Sprachen."""
    _stub_transcript(monkeypatch, RequestBlocked(VIDEO_ID))

    with pytest.raises(ThrottledError) as erhoben:
        fetch(f"https://www.youtube.com/watch?v={VIDEO_ID}")

    exc = erhoben.value
    assert app_module.is_transient(exc) is True
    assert app_module._describe_queued(exc) == (
        "YouTube drosselt gerade die Untertitel. Ich versuche es automatisch später noch einmal."
    )
    assert "keine Untertitel" not in app_module._describe_queued(exc)


def test_the_transcript_session_carries_a_timeout(monkeypatch):
    """Ohne eigenes Zeitlimit hätte der Aufruf gar keines - `requests` wartet dann
    unbegrenzt und die Importstufe mit ihm."""
    captured: dict = {}

    def fake_request(self, method, url, **kwargs):
        captured.update(kwargs)
        return "antwort"

    monkeypatch.setattr(requests.Session, "request", fake_request)

    youtube_module._TimeoutSession().get("https://example.invalid")

    assert captured["timeout"] == youtube_module.TRANSCRIPT_TIMEOUT


# --- Was app.py von diesen Klassen erwartet ------------------------------------------


def test_throttled_error_is_a_source_error():
    # app.py unterscheidet Fehlerarten am Klassennamen (DESIGN.md §7) - ThrottledError
    # muss deshalb eine SourceError-Unterklasse bleiben.
    assert issubclass(ThrottledError, SourceError)


def test_throttled_error_maps_to_the_spec_wording():
    # app.py._describe_source_error muss den wörtlichen Text aus DESIGN.md §7 liefern,
    # nicht "kein Rezept gefunden" (A15).
    exc = ThrottledError("YouTube gibt die Untertitel fuer https://x gerade nicht heraus")
    assert app_module._describe_source_error(exc) == (
        "YouTube drosselt gerade die Untertitel. Bitte später erneut teilen."
    )


def test_no_transcript_error_is_a_source_error():
    # app.py unterscheidet Fehlerarten am Klassennamen (DESIGN.md §7) - NoTranscriptError
    # muss deshalb eine SourceError-Unterklasse bleiben.
    assert issubclass(NoTranscriptError, SourceError)


def test_no_transcript_error_maps_to_the_spec_wording():
    exc = NoTranscriptError("Keine Untertitel (de/en) fuer https://x")
    assert app_module._describe_source_error(exc) == (
        "Das Video hat keine Untertitel, daraus lässt sich kein Rezept lesen."
    )
