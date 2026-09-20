"""Wann ein geparkter Import wieder drankommt (A20,
openspec/changes/add-transient-retry-queue).

Reine Rechnung, kein Ein- und Ausgabeverkehr: `now` und die Zufallsquelle kommen als
Argument herein. Damit ist die Zeitplanung prüfbar, ohne dass ein Test schläft oder
von der Wanduhr abhängt (DESIGN.md §12).

Zwei Stufen, siehe design.md:

1. Exponentieller Rückzug in Minuten, `base * factor ** (attempts - 1)`, mit einer
   Streuung von ±25%. Die Streuung ist kein Beiwerk: mehrere im selben Moment geparkte
   Importe würden sonst im Gleichschritt wiederkommen und dieselbe Überlast erzeugen,
   die sie geparkt hat.
2. Sobald die gerechnete Verzögerung die Schwelle überschreitet, wird nicht weiter
   zurückgezogen, sondern in das nächste Nebenzeitfenster gelegt - an einen zufälligen
   Punkt darin, aus demselben Grund. Läuft das Fenster gerade und passt der Versuch
   noch hinein, gilt das laufende Fenster, nicht das von morgen.

Das Fenster ist lokale Zeit (der Betreiber meint seine Nacht), die gespeicherte
Fälligkeit ist absolute UTC-Zeit wie `created_at`/`updated_at` in `store`. Eine
Zeitumstellung verschiebt damit das Fenster, ohne bereits gespeicherte Fälligkeiten zu
verbiegen.
"""
from __future__ import annotations

import logging
import random
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta

import config

log = logging.getLogger(__name__)

# ±25% Streuung auf die gerechnete Verzögerung.
JITTER_FRACTION = 0.25

# Mindestabstand zwischen "jetzt" und einer Fälligkeit im laufenden Fenster. Ohne ihn
# wäre ein Fenster, das in zehn Sekunden endet, noch ein gültiger Platz - und der
# Versuch liefe faktisch sofort, also genau das, was der Rückzug vermeiden soll.
MIN_WINDOW_SPACING = timedelta(minutes=1)

Jitter = Callable[[], float]


def _window_duration_minutes() -> int:
    """Länge des Fensters, auch über Mitternacht hinweg (`22:00-04:00` sind 360
    Minuten). Die Ränder selbst prüft `config` beim Start."""
    start = config.QUEUE_OFFPEAK_START_MINUTE
    end = config.QUEUE_OFFPEAK_END_MINUTE
    return end - start if end > start else 24 * 60 - start + end


def _offpeak_slot(now: datetime, jitter: Jitter) -> datetime:
    """Ein zufälliger Punkt im nächsten erreichbaren Nebenzeitfenster.

    Geprüft werden gestern, heute und morgen als Beginntag - "gestern" ist nötig, weil
    ein Fenster über Mitternacht (`22:00-04:00`) um 01:00 Uhr am Vortag begonnen hat
    und gerade läuft.

    Die Ränder werden als lokale Wanduhrzeit gebildet und erst dann in eine absolute
    Zeit umgerechnet (`datetime.astimezone()` auf einem zeitzonenlosen Wert legt die
    lokale Zone des jeweiligen Tages an). So bleibt das Fenster nach einer
    Zeitumstellung an derselben Uhrzeit stehen."""
    start_minute = config.QUEUE_OFFPEAK_START_MINUTE
    duration = timedelta(minutes=_window_duration_minutes())
    today_local = now.astimezone().date()

    for offset in (-1, 0, 1):
        day = today_local + timedelta(days=offset)
        start_local = datetime.combine(day, time(start_minute // 60, start_minute % 60))
        start = start_local.astimezone()
        end = (start_local + duration).astimezone()

        earliest = max(start, now + MIN_WINDOW_SPACING)
        if earliest < end:
            span = (end - earliest).total_seconds()
            return (earliest + timedelta(seconds=span * jitter())).astimezone(UTC)

    # Unerreichbar, solange das Fenster eine Länge > 0 hat (das prüft `config` beim
    # Start): spätestens das Fenster von morgen liegt vollständig in der Zukunft.
    raise RuntimeError(f"Kein Nebenzeitfenster erreichbar fuer {now.isoformat()}")


def next_due(attempts: int, now: datetime, jitter: Jitter = random.random) -> datetime:
    """Fälligkeit des nächsten Versuchs, als absolute UTC-Zeit.

    `attempts` ist die Zahl der bereits unternommenen Versuche, also 1 nach dem ersten
    Fehlschlag. `jitter` liefert Werte in [0, 1) - `random.random` im Betrieb, im Test
    ein fester Wert: 0.5 ergibt genau die gerechnete Verzögerung und die Mitte des
    Fensters, 0.0 den frühesten und knapp 1.0 den spätesten Punkt."""
    steps = max(attempts - 1, 0)
    delay = config.QUEUE_BACKOFF_BASE_MINUTES * config.QUEUE_BACKOFF_FACTOR**steps
    delay *= 1 + (jitter() * 2 - 1) * JITTER_FRACTION

    if delay > config.QUEUE_OFFPEAK_THRESHOLD_MINUTES:
        due = _offpeak_slot(now, jitter)
        log.info(
            "Versuch %d: %.1f Minuten Rueckzug ueberschreiten die Schwelle, Nebenzeit %s",
            attempts, delay, due.isoformat(),
        )
        return due

    return (now + timedelta(minutes=delay)).astimezone(UTC)


def _as_datetime(value: str | datetime) -> datetime:
    """`created_at` kommt als ISO-8601-Zeichenkette aus `store`, im Test gelegentlich
    als `datetime`. Ein zeitzonenloser Wert gilt als UTC, wie alles, was `store`
    schreibt."""
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def should_give_up(attempts: int, created_at: str | datetime, now: datetime) -> bool:
    """True, wenn dieser Eintrag nicht mehr wiederholt werden soll.

    Beide Grenzen gelten, die zuerst erreichte gewinnt: die Zahl der Versuche begrenzt
    die Kosten, das Alter seit der Annahme begrenzt, wie lange "später" heissen darf
    (Anforderung "The queue gives up after a bounded number of attempts or a bounded
    age")."""
    if attempts >= config.QUEUE_MAX_ATTEMPTS:
        log.info("Aufgabe nach %d Versuchen (Grenze %d)", attempts, config.QUEUE_MAX_ATTEMPTS)
        return True

    age = now - _as_datetime(created_at)
    if age >= timedelta(hours=config.QUEUE_MAX_AGE_HOURS):
        log.info(
            "Aufgabe nach %.1f Stunden (Grenze %.1f)",
            age.total_seconds() / 3600, config.QUEUE_MAX_AGE_HOURS,
        )
        return True

    return False
