"""Zeitplanung der Warteschlange (Aufgaben 3.1 und 3.2, A20).

Ohne Wanduhr und ohne Zufall: `now` und die Streuquelle sind Argumente. Die
Fenstergrenzen sind lokale Zeit, deshalb rechnen die Fenstertests bewusst über die
lokale Zone statt über feste UTC-Stunden - sonst hinge die Testreihe an der Zeitzone
des Rechners, auf dem sie läuft.
"""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

import config
import schedule


def _local(year, month, day, hour, minute=0) -> datetime:
    """Lokale Wanduhrzeit als absoluter Zeitpunkt - so, wie der Betreiber das Fenster
    meint."""
    return datetime(year, month, day, hour, minute).astimezone()


def _local_time_of(moment: datetime) -> time:
    return moment.astimezone().time()


def _fixed(value: float) -> schedule.Jitter:
    return lambda: value


@pytest.mark.parametrize(
    "attempts, minutes",
    [(1, 2), (2, 6), (3, 18), (4, 54)],
)
def test_backoff_grows_by_the_configured_factor(attempts, minutes):
    """base 2, factor 3: 2, 6, 18, 54 Minuten. Streuung 0.5 ist genau die Mitte, also
    der ungestreute Wert."""
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

    due = schedule.next_due(attempts, now, _fixed(0.5))

    assert due - now == timedelta(minutes=minutes)


def test_first_retry_is_minutes_away_not_seconds():
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

    due = schedule.next_due(1, now, _fixed(0.5))

    assert due - now >= timedelta(minutes=1)


@pytest.mark.parametrize("value, factor", [(0.0, 0.75), (0.5, 1.0), (0.999, 1.25)])
def test_jitter_spans_plus_minus_a_quarter(value, factor):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

    due = schedule.next_due(2, now, _fixed(value))

    expected = timedelta(minutes=6 * factor)
    assert abs((due - now) - expected) < timedelta(seconds=2)


def test_result_is_utc():
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

    assert schedule.next_due(1, now, _fixed(0.5)).tzinfo is UTC


def test_long_backoff_lands_in_the_next_off_peak_window(monkeypatch):
    """Sobald der Rückzug die Schwelle überschreitet, wird nicht weiter verzögert,
    sondern in das Fenster gelegt - hier von 20:00 Uhr aus in die Nacht danach."""
    monkeypatch.setattr(config, "QUEUE_OFFPEAK_START_MINUTE", 2 * 60)
    monkeypatch.setattr(config, "QUEUE_OFFPEAK_END_MINUTE", 6 * 60)
    now = _local(2026, 9, 20, 20)

    # Versuch 5: 2 * 3**4 = 162 Minuten, deutlich über der Schwelle von 60.
    due = schedule.next_due(5, now, _fixed(0.5))

    assert due > now
    assert time(2, 0) <= _local_time_of(due) <= time(6, 0)
    assert due.astimezone().date() == (now.astimezone() + timedelta(days=1)).date()
    # Nicht bloss "weiter zurückgezogen": 162 Minuten nach 20:00 wäre 22:42.
    assert due - now > timedelta(minutes=162)


def test_short_backoff_stays_backoff_even_at_night(monkeypatch):
    monkeypatch.setattr(config, "QUEUE_OFFPEAK_START_MINUTE", 2 * 60)
    monkeypatch.setattr(config, "QUEUE_OFFPEAK_END_MINUTE", 6 * 60)
    now = _local(2026, 9, 20, 23)

    due = schedule.next_due(2, now, _fixed(0.5))

    assert due - now == timedelta(minutes=6)


def test_now_inside_the_window_schedules_into_the_current_one(monkeypatch):
    monkeypatch.setattr(config, "QUEUE_OFFPEAK_START_MINUTE", 2 * 60)
    monkeypatch.setattr(config, "QUEUE_OFFPEAK_END_MINUTE", 6 * 60)
    now = _local(2026, 9, 21, 3)

    due = schedule.next_due(5, now, _fixed(0.5))

    assert due.astimezone().date() == now.astimezone().date()
    assert time(3, 0) <= _local_time_of(due) <= time(6, 0)


def test_window_across_midnight_is_current_after_midnight(monkeypatch):
    """`22:00-04:00` läuft um 01:00 Uhr bereits - das Fenster begann am Vortag."""
    monkeypatch.setattr(config, "QUEUE_OFFPEAK_START_MINUTE", 22 * 60)
    monkeypatch.setattr(config, "QUEUE_OFFPEAK_END_MINUTE", 4 * 60)
    now = _local(2026, 9, 21, 1)

    due = schedule.next_due(5, now, _fixed(0.5))

    assert due - now < timedelta(hours=3)
    assert _local_time_of(due) <= time(4, 0)


def test_end_of_a_running_window_moves_to_the_next_one(monkeypatch):
    """Kurz vor Fensterschluss passt kein Versuch mehr hinein (MIN_WINDOW_SPACING),
    also gilt die nächste Nacht."""
    monkeypatch.setattr(config, "QUEUE_OFFPEAK_START_MINUTE", 2 * 60)
    monkeypatch.setattr(config, "QUEUE_OFFPEAK_END_MINUTE", 6 * 60)
    now = _local(2026, 9, 21, 5, 59)

    due = schedule.next_due(5, now, _fixed(0.5))

    assert due.astimezone().date() == (now.astimezone() + timedelta(days=1)).date()


def test_jitter_spreads_items_inside_the_window(monkeypatch):
    monkeypatch.setattr(config, "QUEUE_OFFPEAK_START_MINUTE", 2 * 60)
    monkeypatch.setattr(config, "QUEUE_OFFPEAK_END_MINUTE", 6 * 60)
    now = _local(2026, 9, 20, 20)

    early = schedule.next_due(5, now, _fixed(0.0))
    late = schedule.next_due(5, now, _fixed(0.99))

    assert late - early > timedelta(hours=3)


def test_give_up_at_the_configured_attempt_count(monkeypatch):
    monkeypatch.setattr(config, "QUEUE_MAX_ATTEMPTS", 5)
    monkeypatch.setattr(config, "QUEUE_MAX_AGE_HOURS", 24)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    created = (now - timedelta(minutes=5)).isoformat()

    assert schedule.should_give_up(4, created, now) is False
    assert schedule.should_give_up(5, created, now) is True


def test_give_up_at_the_configured_age(monkeypatch):
    monkeypatch.setattr(config, "QUEUE_MAX_ATTEMPTS", 5)
    monkeypatch.setattr(config, "QUEUE_MAX_AGE_HOURS", 24)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

    just_inside = (now - timedelta(hours=23, minutes=59)).isoformat()
    just_outside = (now - timedelta(hours=24)).isoformat()

    assert schedule.should_give_up(1, just_inside, now) is False
    assert schedule.should_give_up(1, just_outside, now) is True


def test_created_at_without_a_zone_counts_as_utc(monkeypatch):
    monkeypatch.setattr(config, "QUEUE_MAX_AGE_HOURS", 24)
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

    assert schedule.should_give_up(1, "2026-09-18T12:00:00", now) is True
