"""Namensstufe: aus einem fertigen Rezept einen sprechenden Namen bilden.

Feature A18, `recipe-naming-plan.md`. Der Name kam bisher aus vier Quellen (Mealies
eigener Scraper, JSON-LD der Seite, dem Extraktionsmodell, der Bildueberschrift) und aus
keiner davon verlaesslich brauchbar: Autorennamen ("... von Baerchenknutscher"),
Portalzusaetze, Emojis, Grossschreibung - oder gar kein Name, wenn ein Kamerafoto keine
Ueberschrift zeigt.

Bewusst ein Modellaufruf statt einer Regelmaschine mit Ausschlusslisten: eine Liste aus
"von", "Rezept", "| Chefkoch" waere nie fertig, sie kann den Autorennamen nicht vom
Gerichtnamen ("Toast Hawaii") unterscheiden, und den fehlenden Namen auf dem Kamerafoto
faengt sie gar nicht.

Diese Stufe darf einen Import **nie** scheitern lassen (Plan §2.2). Jeder Fehlschlag
endet in `None` und einer Warnung; der bisherige Name bleibt dann stehen. Kein zweiter
Versuch: in der Extraktionsstufe kostet ein Fehlschlag den ganzen Import, hier nur einen
unschoenen Namen.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import config
import llm
import prompts

log = logging.getLogger(__name__)

# Unter den 120 Sekunden der Extraktion: die Anfrage ist kurz und die Antwort ist ein
# Wort bis sechs (Plan §2.2). Am 2026-08-24 von den geplanten 30 auf 60 angehoben, weil
# ein Lauf gegen gemini-3.6-flash in einen Read-Timeout lief - der Import blieb dabei
# korrekt stehen, trug aber den alten Namen. Die Stufe laeuft im Hintergrund, das Warten
# faellt niemandem auf; ein unnoetig unschoener Name schon.
TIMEOUT_SECONDS = 60

# Laenger ist keine Namenslaenge mehr, sondern Fliesstext (Plan §4).
MAX_NAME_CHARS = 80

# Kuerzungen der Anfrage (Plan §3.2). Keine Sparmassnahme, sondern eine vorhersagbare
# Anfragegroesse: ein Untertiteltranskript von 10 000 Zeichen (A15) ergibt als Rezept
# zehn Schritte, und mehr braucht ein Name nicht.
MAX_INGREDIENTS = 30
MAX_INSTRUCTIONS = 10
MAX_INSTRUCTION_CHARS = 200

# Ein einziges Feld, damit die Antwort nicht in Fliesstext ausufert. Das
# Recipe-Schema aus DESIGN.md §4 passt hier nicht: diese Stufe fasst Zutaten, Schritte,
# Zeiten und Kategorien ausdruecklich nicht an.
NAME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
    "additionalProperties": False,
}


def _shorten(items: list[str], limit: int, chars: int | None = None) -> str:
    lines = []
    for item in items[:limit]:
        text = " ".join(str(item).split())
        if not text:
            continue
        if chars is not None and len(text) > chars:
            text = text[:chars]
        lines.append(text)
    return "\n".join(lines)


def _clean(candidate: str) -> str | None:
    """Nachpruefung aus Plan §4, in Code statt im Prompt.

    Bewusst kein Filter auf einzelne Woerter: heisst ein Gericht wirklich
    "Omas bestes Gulasch", ist das der Name.
    """
    if "\n" in candidate or "\r" in candidate:
        # Dann hat das Modell doch Fliesstext geliefert.
        return None
    name = re.sub(r"\s+", " ", candidate).strip()
    if not name:
        return None
    if len(name) > MAX_NAME_CHARS:
        return None
    return name


def make_name(
    name: str | None,
    source: str,
    ingredients: list[str],
    instructions: list[str],
) -> str | None:
    """Schlaegt einen Namen fuer das Rezept vor.

    Gibt den neuen Namen **nur dann** zurueck, wenn er brauchbar ist *und* sich vom
    vorhandenen unterscheidet. `None` heisst in jedem anderen Fall: alten Namen behalten,
    nichts schreiben. Der haeufigste richtige Ausgang ist "der Name war schon gut", und
    der soll auf dem `import_url`-Weg keinen Schreibzugriff kosten (Plan §4).

    Wirft nie.
    """
    if not config.NAMING_ENABLED:
        log.info("Namensstufe ist per NAMING_ENABLED abgeschaltet, behalte Namen %r", name)
        return None

    current = " ".join((name or "").split())
    messages = [
        {"role": "system", "content": prompts.NAMING_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": prompts.NAMING_USER_PROMPT_TEMPLATE.format(
                # "(keiner)" statt einer leeren Zeile: ein Kamerafoto ohne Ueberschrift
                # hat keinen Namen, und das Modell soll das sehen statt zu raten, ob die
                # Zeile abgeschnitten wurde.
                name=current or "(keiner)",
                source=source,
                ingredients=_shorten(ingredients, MAX_INGREDIENTS),
                instructions=_shorten(instructions, MAX_INSTRUCTIONS, MAX_INSTRUCTION_CHARS),
            ),
        },
    ]

    try:
        content = llm._post(
            messages,
            timeout=TIMEOUT_SECONDS,
            schema=NAME_SCHEMA,
            schema_name="RecipeName",
        )
        data = json.loads(content)
        candidate = data["name"]
        if not isinstance(candidate, str):
            raise ValueError(f"Feld 'name' war {type(candidate).__name__}, nicht str")
    except Exception as exc:  # noqa: BLE001 - Namensstufe darf den Import nie scheitern lassen
        log.warning("Namensstufe fuer %s fehlgeschlagen, behalte Namen %r: %s", source, name, exc)
        return None

    cleaned = _clean(candidate)
    if cleaned is None:
        log.warning("Namensvorschlag fuer %s unbrauchbar, behalte Namen %r", source, name)
        return None

    if cleaned == current:
        log.info("Name %r war bereits gut, keine Aenderung", current)
        return None

    log.info("Name %r wird zu %r", current, cleaned)
    return cleaned
