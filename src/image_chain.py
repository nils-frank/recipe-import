"""Welcher Bildkandidat ist gerade dran, und welche sind gerade draussen.

Feature A22, `openspec/changes/add-image-model-fallback`. Die Bildstufe sprach bis hierher
genau einen Anbieter an (`config.IMAGE_PROVIDER`). Seit auf dem Gemini-Konto Guthaben
liegt, ist die Reihenfolge eine andere: erst die bezahlten Bildmodelle, dann Pollinations
als kostenloser Boden. Ein aufgebrauchtes Guthaben kostet damit Bildqualität statt des
Bildes.

Dieses Modul hält die **Reihenfolge** (`candidates()`) und das **Gedächtnis** darüber,
welcher Kandidat gerade nicht in Frage kommt (`mark_exhausted`, `mark_unknown`). Es ruft
selbst keinen Anbieter auf - das tut `image.generate`, der hier nur fragt, wen er als
nächstes ansprechen soll.

Bewusst gebaut wie `model_chain.py` (A21), bis in die Namen: wer das eine gelesen hat,
kennt das andere. Zwei Unterschiede, beide aus der Sache heraus:

* Der Schlüssel ist das Paar `(Anbieter, Modell)`, nicht der Modellname allein. Derselbe
  Modellname bei zwei Anbietern sind zwei Kandidaten.
* Es gibt keine Modellsuche. Eine Probe kostet bei Textmodellen einen Aufruf, bei
  Bildmodellen ein Bild - `refresh()` hat hier also keine Entsprechung und soll keine
  bekommen (design.md, Non-Goals).

Der Zustand liegt nur im laufenden Prozess; ein Neustart fängt wieder am Kopf der Kette
an. Was innerhalb **eines** Imports schon versucht wurde, steht bewusst nicht hier,
sondern in `image.generate`: das ist keine Aussage über den Kandidaten, sondern über
diesen einen Durchlauf.
"""
from __future__ import annotations

import logging
import threading
import time

import config

log = logging.getLogger(__name__)

Kandidat = tuple[str, str]

# Einzige Zeitquelle des Moduls, damit die Testreihe ohne Schlafen auskommt (DESIGN.md
# §12): ein Test ersetzt `_now` und schiebt die Uhr weiter. `monotonic`, nicht `time`,
# weil eine Sperrfrist nicht davon abhängen darf, ob jemand die Uhr des Rechners stellt.
_now = time.monotonic

# Hintergrundimporte laufen im Threadpool von FastAPI, mehrere können denselben Kandidaten
# im selben Moment als erschöpft melden. Der Lock schützt die beiden Zustände unten; die
# Schreibzugriffe selbst sind idempotent, es gibt kein Lesen-Ändern-Schreiben.
_lock = threading.Lock()

# Kandidat -> monotone Frist, bis zu der er übersprungen wird (Kontingent oder Guthaben
# aufgebraucht).
_cooldown_until: dict[Kandidat, float] = {}

# Kandidaten, deren Modell der Anbieter nicht kennt. Kein Zeitwert: ein Name, den es nicht
# gibt, entsteht nicht durch Warten.
_unknown: set[Kandidat] = set()

# Die Kette in Benutzung. Sie kommt aus der Konfiguration und wird hier nie umsortiert -
# anders als bei den Textmodellen gibt es keine Modellsuche, die einen neueren Kopf
# voranstellen dürfte.
_chain: list[Kandidat] = [tuple(eintrag) for eintrag in config.IMAGE_MODEL_CHAIN]


def candidates() -> list[Kandidat]:
    """Die Kandidaten, die jetzt in Frage kommen, bevorzugter zuerst.

    Eine **leere** Liste ist ein gültiger Zustand, keine Ausnahme: sie heisst "alles
    gesperrt oder unbekannt". Der Aufrufer macht daraus die Protokollzeile, die zu seinem
    Fall passt, und der Import läuft ohne Bild weiter.
    """
    with _lock:
        jetzt = _now()
        return [
            kandidat
            for kandidat in _chain
            if kandidat not in _unknown and _cooldown_until.get(kandidat, 0.0) <= jetzt
        ]


def configured() -> list[Kandidat]:
    """Die Kette ohne jede Filterung. Für die Protokollzeile, die eine leere
    Kandidatenliste deutet: alles gesperrt heisst "später nochmal", alles unbekannt heisst
    "falsch konfiguriert" - zwei verschiedene Meldungen."""
    with _lock:
        return list(_chain)


def mark_exhausted(kandidat: Kandidat) -> None:
    """Dieser Kandidat hat sein Kontingent, sein Guthaben oder sein Ratenlimit als
    aufgebraucht gemeldet.

    Er wird für `IMAGE_MODEL_COOLDOWN_SECONDS` übersprungen. Zweck ist nicht die
    Genauigkeit - ein leeres Guthaben füllt sich nicht nach einer Stunde - sondern die
    Kosten: so kostet es einen abgewiesenen Aufruf je Sperrfrist statt einen je Import.
    """
    with _lock:
        _cooldown_until[kandidat] = _now() + config.IMAGE_MODEL_COOLDOWN_SECONDS
    log.warning(
        "Bildkandidat %s:%s meldet sich als erschöpft, er wird %.0fs übersprungen",
        kandidat[0], kandidat[1], config.IMAGE_MODEL_COOLDOWN_SECONDS,
    )


def mark_unknown(kandidat: Kandidat) -> None:
    """Der Anbieter kennt dieses Modell nicht (404 oder ein 400, das den Namen nennt).

    Für die Lebensdauer des Prozesses draussen, unabhängig von jeder Uhr. Bei Bildmodellen
    ist das kein gedachter Fall: die Namen tragen `-preview`-Varianten, die verschwinden,
    und ein zurückgezogener Name würde sonst je Import einen Aufruf kosten.
    """
    with _lock:
        _unknown.add(kandidat)
    log.warning(
        "Bildkandidat %s:%s ist dem Anbieter unbekannt, er fällt aus der Kette",
        kandidat[0], kandidat[1],
    )


def reset() -> None:
    """Zustand auf die Konfiguration zurücksetzen. Nur für die Testreihe - im Dienst gibt
    es dafür den Neustart."""
    global _chain
    with _lock:
        _cooldown_until.clear()
        _unknown.clear()
        _chain = [tuple(eintrag) for eintrag in config.IMAGE_MODEL_CHAIN]
