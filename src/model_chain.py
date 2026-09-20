"""Welches Textmodell ist gerade dran, und welche sind gerade draussen.

Feature A21, `openspec/changes/add-llm-model-fallback`. Der Dienst sprach bis hierher
genau ein Modell an (`config.LLM_MODEL`). Antwortete es 429, starb der Import - obwohl
dasselbe Konto auf einem anderen Modell weiter antwortet. Gemessen am 2026-09-20:
`gemini-3.6-flash` antwortete 429 "You exceeded your current quota", `gemini-3.8-flash`
und `gemini-3.5-flash` im selben Moment 200.

Dieses Modul hält deshalb die **Reihenfolge** (`candidates()`) und das **Gedächtnis**
darüber, welches Modell gerade nicht in Frage kommt (`mark_exhausted`, `mark_unknown`).
Es spricht selbst kein Modell an - das tut `llm._post`, der hier nur fragt, welchen
Namen er als nächstes einsetzen soll. Einzige Ausnahme ist `refresh()`, die Modellsuche.

Getrennt von `llm.py`, weil das zwei Belange sind: dort Transport, Schema und Parsen,
hier Reihenfolge, Uhren und Sperrfristen. Der Zustand liegt nur im laufenden Prozess;
ein Neustart fängt wieder am Kopf der Kette an (design.md, Entscheidungen).

`refresh()` ist der zweite Teil: einmal beim Start und danach täglich liest der Dienst
die Modellliste des Anbieters und stellt ein neueres Modell der Kette voran - aber erst,
nachdem es eine schemaerzwungene Probe bestanden hat. Das kehrt die Regel aus
DESIGN.md §3 um ("fest verdrahtet, kein wandernder Alias"), deshalb die Probe, der
Protokolleintrag, die eine Push-Meldung und `LLM_MODEL_AUTODISCOVER=false`.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

import requests

import config

log = logging.getLogger(__name__)

# Einzige Zeitquelle des Moduls, damit die Testreihe ohne Schlafen auskommt (DESIGN.md
# §12): ein Test ersetzt `_now` und schiebt die Uhr weiter. `monotonic`, nicht `time`,
# weil eine Sperrfrist von einer Stunde nicht davon abhängen darf, ob jemand die Uhr des
# Rechners stellt.
_now = time.monotonic

# Hintergrundimporte laufen im Threadpool von FastAPI, mehrere können dasselbe Modell im
# selben Moment als erschöpft melden. Der Lock schützt die drei Zustände unten; die
# Schreibzugriffe selbst sind idempotent, es gibt kein Lesen-Ändern-Schreiben.
_lock = threading.Lock()

# Modell -> monotone Frist, bis zu der es übersprungen wird (429 zweimal).
_cooldown_until: dict[str, float] = {}

# Modelle, die der Anbieter nicht kennt (404 / model-not-found 400). Kein Zeitwert:
# ein Name, den es nicht gibt, entsteht nicht durch Warten.
_unknown: set[str] = set()

# Modelle, die der letzte Durchlauf der Modellsuche nicht in der Liste des Anbieters
# gefunden hat. Absichtlich getrennt von `_unknown` und bei jedem Durchlauf neu gesetzt:
# das ist eine Aussage der Liste von eben, keine Ablehnung auf Lebenszeit. Steht der
# Name im nächsten Durchlauf wieder drin, ist er wieder dabei.
_nicht_gelistet: set[str] = set()

# Die Kette in Benutzung. Startet bei der Konfiguration; die Modellsuche darf ihr einen
# neueren Kopf voranstellen (`refresh`), aber nie einen konfigurierten Namen entfernen
# oder die konfigurierten untereinander umsortieren.
_chain: list[str] = list(config.LLM_MODEL_CHAIN)


def candidates() -> list[str]:
    """Die Modelle, die jetzt in Frage kommen, bevorzugtes zuerst.

    Eine **leere** Liste ist ein gültiger Zustand, keine Ausnahme: sie heisst "alles
    gesperrt oder unbekannt". Der Aufrufer (`llm._post`) macht daraus die Meldung, die
    zu seinem Fall passt - nur er weiss, ob gerade ein Import oder eine Probe läuft.
    """
    with _lock:
        jetzt = _now()
        return [
            model
            for model in _chain
            if model not in _unknown
            and model not in _nicht_gelistet
            and _cooldown_until.get(model, 0.0) <= jetzt
        ]


def head() -> str | None:
    """Das bevorzugte Modell, für Protokollzeilen. `None`, wenn gerade keines übrig ist."""
    eligible = candidates()
    return eligible[0] if eligible else None


def configured_head() -> str | None:
    """Der Kopf der Kette ohne Rücksicht auf Sperrfristen - also das Modell, das dieser
    Dienst normalerweise nimmt. Die Modellsuche vergleicht dagegen, nicht gegen `head()`:
    sonst würde eine zufällig gesperrte Spitze als "veraltet" gelesen."""
    with _lock:
        return _chain[0] if _chain else None


def mark_exhausted(model: str) -> None:
    """Ein Modell hat zweimal 429 geantwortet: sein Kontingent ist aufgebraucht.

    Es wird für `LLM_MODEL_COOLDOWN_SECONDS` übersprungen. Zweck ist nicht die
    Genauigkeit - ein Tageskontingent endet zu einer Uhrzeit, nicht nach einer Frist -
    sondern die Kosten: so kostet ein leeres Kontingent einen abgelehnten Aufruf je
    Sperrfrist statt einen je Import.
    """
    with _lock:
        _cooldown_until[model] = _now() + config.LLM_MODEL_COOLDOWN_SECONDS
    log.warning(
        "Modell %s ist erschöpft (429), es wird %.0fs übersprungen",
        model, config.LLM_MODEL_COOLDOWN_SECONDS,
    )


def mark_unknown(model: str) -> None:
    """Der Anbieter kennt dieses Modell nicht (404 oder ein 400, das den Namen nennt).

    Für die Lebensdauer des Prozesses draussen, unabhängig von jeder Uhr. Gemessen am
    2026-09-20 ist das kein gedachter Fall: `gemini-2.5-flash` antwortet
    404 "no longer available to new users" - ein Name, der gestern noch ging.
    """
    with _lock:
        _unknown.add(model)
    log.warning("Modell %s kennt der Anbieter nicht, es fällt aus der Kette", model)


def configured() -> list[str]:
    """Die Kette in Benutzung, ohne jede Filterung. `llm._post` braucht sie, um eine
    leere Kandidatenliste zu deuten: alles gesperrt heisst "später nochmal", alles
    unbekannt heisst "falsch konfiguriert" - zwei verschiedene Meldungen."""
    with _lock:
        return list(_chain)


def unknown() -> set[str]:
    """Die Namen, die der Anbieter nicht führt - abgewiesene *und* nicht gelistete.

    Für den Aufrufer ist das derselbe Fall: kein Aufruf mit diesem Namen kann gelingen.
    Die Trennung der beiden Mengen ist innen wichtig (die eine gilt für den Prozess, die
    andere nur bis zum nächsten Durchlauf der Modellsuche), draussen nicht.
    """
    with _lock:
        return _unknown | _nicht_gelistet


def reset() -> None:
    """Zustand auf die Konfiguration zurücksetzen. Nur für die Testreihe - im Dienst
    gibt es dafür den Neustart."""
    global _chain
    with _lock:
        _cooldown_until.clear()
        _unknown.clear()
        _nicht_gelistet.clear()
        _abgelehnt.clear()
        _gemeldet.clear()
        _chain = list(config.LLM_MODEL_CHAIN)


# ---------------------------------------------------------------------------
# Modellsuche: Liste lesen, neuestes Modell proben, übernehmen, einmal melden.
# ---------------------------------------------------------------------------

# Kurz: die Liste ist ein paar Kilobyte, und niemand wartet auf sie. Ein Durchlauf, der
# hier hängt, darf den Start nicht aufhalten (siehe app.lifespan).
_LIST_TIMEOUT_SECONDS = 15

# Die Probe ist ein Satz hin und ein Feld zurück. Deutlich unter den 120 Sekunden der
# Extraktion, weil ein Modell, das dafür eine halbe Minute braucht, für diesen Dienst
# ohnehin nicht in Frage kommt.
_PROBE_TIMEOUT_SECONDS = 30

# Ein einziges Pflichtfeld. Geprüft wird nicht die Antwort, sondern der Vertrag: kann
# dieses Modell `response_format: json_schema` mit `strict`? Genau daran hängt die
# ganze Textstufe - ein Modell, das das nicht kann, würde jeden Import zerbrechen.
_PROBE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
_PROBE_MESSAGES = [{"role": "user", "content": "Antworte mit ok=true."}]

# Modelle, die eine Probe nicht bestanden haben. Sie werden im selben Durchlauf nicht
# noch einmal geprobt; der nächste Durchlauf versucht es erneut, weil ein 503 von
# gestern nichts über heute sagt.
_abgelehnt: set[str] = set()

# Köpfe, über die schon eine Push-Meldung hinausging - damit ein täglicher Durchlauf
# nicht täglich dasselbe meldet.
_gemeldet: set[str] = set()


def _version(model: str, muster: re.Pattern[str]) -> tuple[int, int] | None:
    """Haupt- und Nebenversion als Zahlen. Als Zahlen, nicht als Text: sonst stünde
    3.10 unter 3.9."""
    treffer = muster.match(model)
    if not treffer:
        return None
    try:
        return int(treffer.group(1)), int(treffer.group(2))
    except (IndexError, ValueError):
        return None


def _list_models() -> list[str] | None:
    """Die Modellnamen des Anbieters, `models/`-Vorsatz entfernt.

    `None` heisst "nicht lesbar" und ist von einer leeren Liste unterschieden: das eine
    ist ein Fehlschlag, das andere eine Aussage. Ein Versuch, kein Wiederholen - der
    nächste Durchlauf kommt ohnehin.
    """
    url = config.LLM_BASE_URL.rstrip("/") + "/models"
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
            timeout=_LIST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        log.warning("Modellliste nicht erreichbar, die Kette bleibt wie sie ist: %s", exc)
        return None

    if response.status_code != 200:
        log.warning(
            "Modellliste antwortete mit HTTP %d, die Kette bleibt wie sie ist: %s",
            response.status_code, response.text[:200],
        )
        return None

    try:
        data = response.json()
        eintraege = data["data"]
        namen = [str(eintrag["id"]) for eintrag in eintraege]
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        log.warning("Modellliste war unlesbar, die Kette bleibt wie sie ist: %s", exc)
        return None

    # Gemessen am 2026-09-20: auf der OpenAI-kompatiblen Fläche tragen die Namen den
    # Vorsatz ("models/gemini-3.8-flash"), im Aufruf selbst darf er nicht stehen.
    return [name.removeprefix("models/") for name in namen]


def _probe(model: str) -> bool:
    """Ein einziger schemaerzwungener Aufruf. Bestanden heisst: 200 **und** eine
    Antwort, die zum Schema passt.

    Der Import von `llm` steht hier und nicht oben im Modul, weil `llm` seinerseits
    `model_chain` importiert - ein Ringimport beim Laden. Zur Aufrufzeit ist er
    harmlos.
    """
    import llm

    try:
        content = llm._post(
            _PROBE_MESSAGES,
            timeout=_PROBE_TIMEOUT_SECONDS,
            schema=_PROBE_SCHEMA,
            schema_name="Probe",
            model=model,
        )
        daten = json.loads(content)
        if not isinstance(daten, dict) or not isinstance(daten.get("ok"), bool):
            raise ValueError(f"Antwort passte nicht zum Probeschema: {content[:120]}")
    except Exception as exc:  # noqa: BLE001 - jeder Fehlschlag der Probe zaehlt gleich
        # Jede Art von Fehlschlag zählt gleich: Stufe, Zeitüberschreitung, unlesbar
        # oder schemawidrig. Übernommen wird nur, was den Vertrag vorgeführt hat.
        log.warning("Modell %s hat die Probe nicht bestanden, es wird nicht übernommen: %s", model, exc)
        return False

    log.info("Modell %s hat die Probe bestanden", model)
    return True


def refresh() -> None:
    """Ein Durchlauf der Modellsuche. Wirft nie - ein Fehlschlag lässt die Kette stehen.

    Der Ablauf steht in design.md: Liste lesen, konfigurierte Namen dagegen halten,
    das neueste passende Modell bestimmen, und wenn es neuer ist als der jetzige Kopf,
    einmal proben und voranstellen.
    """
    if not config.LLM_MODEL_AUTODISCOVER:
        log.info("Modellsuche ist per LLM_MODEL_AUTODISCOVER abgeschaltet")
        return

    try:
        _refresh()
    except Exception as exc:  # noqa: BLE001 - Modellsuche darf den Dienst nie mitnehmen  # pragma: no cover
        # Letzte Sicherung: diese Stufe läuft im Hintergrund und darf den Dienst unter
        # keinen Umständen mitnehmen.
        log.warning("Modellsuche fehlgeschlagen, die Kette bleibt wie sie ist: %s", exc)


def _refresh() -> None:
    global _chain

    angeboten = _list_models()
    if angeboten is None:
        return
    if not angeboten:
        log.warning("Modellliste war leer, die Kette bleibt wie sie ist")
        return

    try:
        muster = re.compile(config.LLM_MODEL_PATTERN)
    except re.error as exc:
        log.warning("LLM_MODEL_PATTERN ist kein gültiger Ausdruck (%s), die Kette bleibt wie sie ist", exc)
        return

    # Konfigurierte Namen gegen die Liste halten. Das ist die billige Art, einen
    # zurückgezogenen Namen zu finden, bevor ein Import über ihn stolpert.
    fehlend = {model for model in configured() if model not in angeboten}
    if fehlend:
        log.warning(
            "Der Anbieter führt diese eingestellten Modelle nicht: %s - sie werden übersprungen",
            ", ".join(sorted(fehlend)),
        )
    with _lock:
        # Ersetzen, nicht ergänzen: was diesmal wieder in der Liste steht, ist wieder
        # dabei (Spezifikation: "skipped while the list says so").
        _nicht_gelistet.clear()
        _nicht_gelistet.update(fehlend)

    passend = sorted(
        ((v, m) for m in angeboten if (v := _version(m, muster)) is not None),
        reverse=True,
    )
    if not passend:
        log.info("Die Modellliste enthält kein Modell, das zu LLM_MODEL_PATTERN passt")
        return

    neuestes_version, neuestes = passend[0]
    kopf = configured_head()
    kopf_version = _version(kopf, muster) if kopf else None

    if kopf is not None and kopf_version is not None and neuestes_version <= kopf_version:
        log.info("Kein neueres Modell als %s im Angebot, die Kette bleibt wie sie ist", kopf)
        return
    if neuestes == kopf:
        return
    if neuestes in _abgelehnt:
        log.info("Modell %s ist in diesem Durchlauf schon durchgefallen", neuestes)
        return

    if not _probe(neuestes):
        with _lock:
            _abgelehnt.add(neuestes)
        return

    with _lock:
        rest = [m for m in _chain if m != neuestes]
        _chain = [neuestes, *rest]
        # Ein übernommenes Modell startet ohne Altlasten: eine Sperrfrist aus der Zeit,
        # als es noch nicht in der Kette stand, wäre nicht seine.
        _cooldown_until.pop(neuestes, None)
        _unknown.discard(neuestes)
        _nicht_gelistet.discard(neuestes)
        _abgelehnt.discard(neuestes)
        schon_gemeldet = neuestes in _gemeldet
        _gemeldet.add(neuestes)

    log.warning("Neues Textmodell übernommen: %s löst %s als bevorzugtes Modell ab", neuestes, kopf)

    if not schon_gemeldet:
        _melde(kopf, neuestes)


def _melde(alt: str | None, neu: str) -> None:
    """Genau eine Push-Meldung je übernommenem Modell.

    Sie ist der Preis dafür, dass der Modellwechsel nicht mehr von Hand geschieht: wenn
    Namen oder Rezepte plötzlich anders klingen, steht der Grund im Telefon statt nur im
    Protokoll. `ha_notify.notify` wirft nie, hier trotzdem abgesichert - eine Meldung
    darf die Kette nicht mitnehmen.
    """
    import ha_notify

    try:
        ha_notify.notify(
            "Neues Sprachmodell",
            f"recipe-import nutzt ab jetzt {neu} statt {alt or 'dem bisherigen Modell'}.",
        )
    except Exception as exc:  # noqa: BLE001 - eine Meldung darf die Kette nie mitnehmen  # pragma: no cover
        log.warning("Meldung über den Modellwechsel fehlgeschlagen: %s", exc)
