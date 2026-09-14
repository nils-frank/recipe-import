"""Rezeptquelle fuer gewoehnliche Webseiten.

Drei Stufen, in dieser Reihenfolge (DESIGN.md Abschnitt 6): `recipe-scrapers`
kennt viele Rezeptseiten direkt und liefert bereits ein vollstaendiges
`Recipe`. Kennt es die Domain nicht oder bleibt das Ergebnis unvollstaendig,
wird eingebettetes JSON-LD vom Typ `Recipe` gesucht (`extruct`). Erst wenn
auch das nichts Verwertbares liefert, geht `trafilatura`-Fliesstext an die
LLM-Stufe (llm.py, A2).

Die HTML-Seite wird genau einmal geladen und fuer alle drei Stufen
wiederverwendet - das haelt es bei einem HTTP-Aufruf pro Import statt drei.

Dieses Modul ruft niemals Mealie oder das LLM auf, es liest nur.
"""
from __future__ import annotations

import logging

import extruct
import requests
import trafilatura
from pydantic import ValidationError
from recipe_scrapers import (
    NoSchemaFoundInWildMode,
    WebsiteNotImplementedError,
    scrape_html,
)

from schema import Recipe
from sources import SourceError, SourceResult

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30  # Sekunden, siehe DESIGN.md Abschnitt 6

# Realistischer Desktop-Browser-User-Agent. Ohne ihn blocken einige
# Rezeptseiten den Standard-User-Agent von `requests` mit 403.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _fetch_html(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    log.info("GET %s -> %d (%d bytes)", url, resp.status_code, len(resp.content))
    resp.raise_for_status()
    return resp.text


def _minutes_to_iso8601_duration(minutes) -> str | None:
    """z.B. 90 -> 'PT1H30M'. None/0/unparbar -> None statt einer erfundenen Zeit."""
    if minutes is None:
        return None
    try:
        total_minutes = int(minutes)
    except (TypeError, ValueError):
        return None
    if total_minutes <= 0:
        return None
    hours, mins = divmod(total_minutes, 60)
    if hours and mins:
        return f"PT{hours}H{mins}M"
    if hours:
        return f"PT{hours}H"
    return f"PT{mins}M"


def _safe(fn):
    """recipe-scrapers wirft NotImplementedError/ElementNotFoundInHtml etc., wenn ein
    einzelnes Feld auf der jeweiligen Seite fehlt. Das ist normal, kein Abbruchgrund."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - bewusst breit, siehe Docstring
        log.debug("recipe-scrapers: Feld nicht verfuegbar (%s)", exc)
        return None


def _from_scrapers(html: str, url: str) -> Recipe | None:
    try:
        scraper = scrape_html(html, url)
    except (WebsiteNotImplementedError, NoSchemaFoundInWildMode) as exc:
        log.info("recipe-scrapers: Domain nicht unterstuetzt fuer %s (%s)", url, exc)
        return None

    category = _safe(scraper.category)
    categories = [category] if isinstance(category, str) and category.strip() else []

    yields = _safe(scraper.yields)

    try:
        return Recipe(
            name=_safe(scraper.title) or "",
            recipeIngredient=_safe(scraper.ingredients) or [],
            recipeInstructions=_safe(scraper.instructions_list) or [],
            recipeYield=str(yields) if yields else None,
            totalTime=_minutes_to_iso8601_duration(_safe(scraper.total_time)),
            description=_safe(scraper.description),
            recipeCategory=categories,
            url=url,
        )
    except ValidationError as exc:
        log.info("recipe-scrapers: Ergebnis fuer %s unvollstaendig (%s)", url, exc)
        return None


def _find_recipe_node(node):
    """Sucht rekursiv nach einem JSON-LD-Knoten mit @type Recipe, auch innerhalb
    von @graph-Bloecken, wie sie viele Rezept-Plugins (WordPress u.a.) erzeugen."""
    if isinstance(node, dict):
        types = node.get("@type")
        if isinstance(types, str):
            types = [types]
        if isinstance(types, list) and "Recipe" in types:
            return node
        graph = node.get("@graph")
        if isinstance(graph, list):
            for child in graph:
                found = _find_recipe_node(child)
                if found is not None:
                    return found
    elif isinstance(node, list):
        for child in node:
            found = _find_recipe_node(child)
            if found is not None:
                return found
    return None


def _text_of(value) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("name") or value.get("text")
    return None


def _list_of_str(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for s in (_text_of(v) for v in value) if s]
    return []


def _instructions_of(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip() for line in value.split("\n") if line.strip()]
    if isinstance(value, list):
        steps: list[str] = []
        for item in value:
            if isinstance(item, str):
                steps.append(item)
            elif isinstance(item, dict):
                if item.get("@type") == "HowToSection":
                    steps.extend(_instructions_of(item.get("itemListElement")))
                else:
                    text = item.get("text") or item.get("name")
                    if text:
                        steps.append(text)
        return steps
    return []


def _from_jsonld(html: str, url: str) -> Recipe | None:
    try:
        data = extruct.extract(html, base_url=url, syntaxes=["json-ld"], uniform=True)
    except Exception as exc:  # noqa: BLE001 - extruct kann bei kaputtem HTML alles werfen
        log.info("extruct: JSON-LD-Extraktion fehlgeschlagen fuer %s (%s)", url, exc)
        return None

    node = None
    for block in data.get("json-ld") or []:
        node = _find_recipe_node(block)
        if node is not None:
            break
    if node is None:
        return None

    total_time = node.get("totalTime")
    if not (isinstance(total_time, str) and total_time.startswith("P")):
        total_time = None

    recipe_yield = node.get("recipeYield")
    if isinstance(recipe_yield, list):
        recipe_yield = recipe_yield[0] if recipe_yield else None
    recipe_yield = str(recipe_yield) if recipe_yield else None

    try:
        return Recipe(
            name=_text_of(node.get("name")) or "",
            recipeIngredient=_list_of_str(node.get("recipeIngredient")),
            recipeInstructions=_instructions_of(node.get("recipeInstructions")),
            recipeYield=recipe_yield,
            totalTime=total_time,
            description=_text_of(node.get("description")),
            recipeCategory=_list_of_str(node.get("recipeCategory")),
            url=url,
        )
    except ValidationError as exc:
        log.info("JSON-LD-Rezept fuer %s unvollstaendig (%s)", url, exc)
        return None


def _from_text(html: str, url: str) -> tuple[str | None, str | None]:
    text = trafilatura.extract(html, url=url, include_comments=False, include_tables=False)
    title = None
    try:
        meta = trafilatura.extract_metadata(html, default_url=url)
        if meta is not None:
            title = meta.title
    except Exception as exc:  # noqa: BLE001 - Metadaten sind ein Bonus, kein Abbruchgrund
        log.debug("trafilatura: Metadaten-Extraktion fehlgeschlagen fuer %s (%s)", url, exc)
    return text, title


def fetch(url: str) -> SourceResult:
    html = _fetch_html(url)

    recipe = _from_scrapers(html, url)
    if recipe is not None:
        log.info("site: Rezept per recipe-scrapers gefunden (%s)", url)
        return SourceResult(recipe=recipe, text=None, title=recipe.name, stage="scrapers")

    recipe = _from_jsonld(html, url)
    if recipe is not None:
        log.info("site: Rezept per JSON-LD gefunden (%s)", url)
        return SourceResult(recipe=recipe, text=None, title=recipe.name, stage="jsonld")

    text, title = _from_text(html, url)
    if not text or not text.strip():
        raise SourceError(f"Kein Rezept und kein Fliesstext extrahierbar: {url}")

    log.info("site: Fliesstext per trafilatura extrahiert (%s, %d Zeichen)", url, len(text))
    return SourceResult(recipe=None, text=text, title=title, stage="text")
