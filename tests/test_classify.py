"""Tests für classify.py (DESIGN.md §12, Punkte 1 und 2)."""
from __future__ import annotations

import pytest

from classify import classify, normalize_url, url_hash

_VIDEO_ID = "dQw4w9WgXcQ"
_CANONICAL = f"https://www.youtube.com/watch?v={_VIDEO_ID}"


@pytest.mark.parametrize(
    "raw",
    [
        f"https://youtu.be/{_VIDEO_ID}",
        f"https://www.youtube.com/watch?v={_VIDEO_ID}",
        f"https://www.youtube.com/watch?v={_VIDEO_ID}&t=42s",
        f"https://www.youtube.com/shorts/{_VIDEO_ID}",
        f"https://m.youtube.com/watch?v={_VIDEO_ID}",
    ],
)
def test_normalize_url_all_youtube_forms(raw):
    assert normalize_url(raw) == _CANONICAL


def test_normalize_url_strips_tracking_params():
    raw = (
        "https://example.com/rezepte/apfelkuchen/"
        "?utm_source=newsletter&utm_medium=email&fbclid=abc123&gclid=xyz789&si=trackme"
    )
    assert normalize_url(raw) == "https://example.com/rezepte/apfelkuchen"


def test_normalize_url_keeps_non_tracking_params():
    raw = "https://example.com/rezepte/apfelkuchen?portionen=4&utm_source=x"
    assert normalize_url(raw) == "https://example.com/rezepte/apfelkuchen?portionen=4"


def test_normalize_url_strips_fragment():
    assert (
        normalize_url("https://example.com/rezepte/apfelkuchen#zutaten")
        == "https://example.com/rezepte/apfelkuchen"
    )


def test_url_hash_equal_for_differently_written_same_url():
    # Fassung 1: abschliessender Schrägstrich, unsortierte Tracking-Parameter.
    a = url_hash("https://example.com/rezepte/apfelkuchen/?utm_source=newsletter&b=2&a=1")
    # Fassung 2: kein Schrägstrich, Tracking-Parameter bereits entfernt, sortiert.
    b = url_hash("https://example.com/rezepte/apfelkuchen?a=1&b=2")
    assert a == b


def test_url_hash_differs_for_different_urls():
    a = url_hash("https://example.com/rezepte/apfelkuchen")
    b = url_hash("https://example.com/rezepte/birnenkuchen")
    assert a != b


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://example.com/rezepte/apfelkuchen", "site"),
        (f"https://youtu.be/{_VIDEO_ID}", "youtube"),
        (_CANONICAL, "youtube"),
        ("ftp://example.com/datei", "unsupported"),
        ("nicht-mal-eine-url", "unsupported"),
    ],
)
def test_classify(url, expected):
    assert classify(url) == expected
