"""Mealie-API-Client: URL-Import, JSON-LD-Import, Tags, Oberflächen-Link.

Pfade und Antwortformen live gegen die laufende Anlage verifiziert am 2026-08-22
via `curl -s http://mealie.local:30081/openapi.json | jq -r '.paths | keys[]' |
grep -i recipe`:

- URL-Import:   POST /api/recipes/create/url            Body {"url": ...}
                Antwort 201, Körper ist der Slug als reiner JSON-String.
- JSON-Import:  POST /api/recipes/create/html-or-json    Body {"data": "<JSON-LD als
                String>"}. Das "data"-Feld ist laut Schema `ScrapeRecipeData` ein
                String, kein verschachteltes Objekt - deshalb json.dumps(data) vor
                dem Senden. Antwort ebenfalls 201 mit dem Slug als JSON-String.
- Tags:         PATCH /api/recipes/{slug}                Body {"tags": [...]}. Die
                Alternative POST /api/recipes/bulk-actions/tag verlangt laut Schema
                `AssignTags` -> `TagBase` bereits existierende Tag-UUIDs, ist also für
                ein neu angelegtes "auto-import"-Tag ungeeignet. PATCH akzeptiert laut
                Schema `Recipe-Input` ein `RecipeTag` mit nur `name` und `slug` (keine
                ID nötig) und legt das Tag bei Bedarf neu an. `Recipe-Input` hat keine
                Pflichtfelder, ein PATCH mit nur "tags" ändert daher nichts sonst.

Name zum Slug: `GET /api/recipes/{slug}` - live verifiziert am 2026-08-23 über
`/openapi.json` (Pfad, Methode GET, Antwortschema `Recipe-Output` mit Feld "name") und
per `curl` ohne Token (Antwort 401, Auth also Pflicht). Siehe `get_recipe_name`,
Reparatur zu Review-A8-Befund 2.

Oberflächen-Link: live verifiziert am 2026-08-22 über ein vorhandenes Rezept aus
`GET /api/explore/groups/home/recipes` (öffentlich, kein Token nötig) mit dem Slug
`kartoffel-gemuse-suppe-mit-wurstchen-und-kloschen`. `GET /api/app/about` liefert
`defaultGroupSlug: "home"`. Die Form `{MEALIE_URL}/g/home/r/{slug}` antwortete mit
HTTP 200 und dem SPA-Titel "Mealie", ein frei erfundener Slug in derselben Form dagegen
mit HTTP 404 - die Prüfung ist also serverseitig und kein blosser SPA-Fallback. Die
Gruppen-Slug-Konfiguration fehlt in DESIGN.md §3, deshalb hier als Konstante mit Datum
der Live-Prüfung festgehalten, nicht als eigene Umgebungsvariable erfunden.

Platzhalter-Rezepte (A16, Befund aus A10 vom 2026-08-23): `POST /api/recipes/create/url`
liefert für eine erreichbare Seite ohne Rezept keinen Fehlschlag, sondern legt ein
Rezept mit dem Seitentitel und Sentinel-Inhalten an. Live verifiziert am 2026-08-24 gegen
`mealie.local:30081` mit `https://de.wikipedia.org/wiki/Kartoffel`: `create/url`
antwortet 201, `GET /api/recipes/{slug}` liefert dazu genau einen Eintrag in
`recipeIngredient` mit `"note": "Could not detect ingredients"` und genau einen Eintrag
in `recipeInstructions` mit `"text": "Could not detect instructions"` - keine leeren
Listen, wie man ohne diese Prüfung vermuten würde. Zum Vergleich lieferte dieselbe Probe
für die Chefkoch-Testseite aus `test_files/test_url.txt` 17 Zutaten und 13 Schritte mit
echtem Text. Siehe `is_placeholder`.

Bildstufe (A19, openspec/changes/add-ai-recipe-image), live geprüft am 2026-09-20 gegen
Mealie v3.22.0 (Aufgaben 1.1 und 1.2 der Änderung):

- Bild setzen (Aufgabe 1.1): `PUT /api/recipes/{slug}/image` existiert, neben `post` und
  `delete` auf demselben Pfad. `/openapi.json` nennt als Körper `multipart/form-data` nach
  dem Schema `Body_update_recipe_image_api_recipes__slug__image_put` mit genau zwei
  Feldern, **beide Pflicht**: `image` (`contentMediaType: application/octet-stream`) und
  `extension` (String). Antworten: `200` bei Erfolg, `422` bei Schemafehler - kein `201`.
  Als einziger Aufruf hier kein JSON, deshalb ohne `_HEADERS`: `requests` muss
  Content-Type samt boundary selbst setzen.

- Bild vorhanden (Aufgabe 1.2): **das Feld `image` taugt dafür nicht.** Gemessen über alle
  40 Rezepte der Anlage: 29 haben keine Bilddatei, aber nur ein einziges (`kasespatzle`)
  trägt `image: null`. Die anderen 28 tragen eine zufällige Kennung wie `'QziM'`, obwohl
  unter `GET /api/media/recipes/{id}/images/original.webp` nichts liegt (404, auch für
  `min-original.webp` und `tiny-original.webp`). Mealie vergibt die Kennung offenbar beim
  Anlegen, nicht beim Hinterlegen eines Bildes; sie ist ein Cache-Schlüssel, keine Aussage
  über ein vorhandenes Bild. Belastbar ist nur die Mediendatei selbst: `GET
  /api/media/recipes/{id}/images/original.webp` antwortete für die 11 Rezepte mit Bild mit
  `200 image/webp` (z.B. `quiche-lorraine-rezept-der-klassiker`, 85690 Bytes) und sonst
  mit `404`.

  `has_image()` unten wertet noch das Feld aus und meldet damit für ein bildloses Rezept
  "hat ein Bild". Das ist der Befund aus Aufgabe 1.2 und macht Aufgabe 2.2 der Änderung
  neu auf; hier steht bewusst die Messung und nicht schon die Umarbeitung.
"""
from __future__ import annotations

import json
import logging

import requests

from config import MEALIE_TOKEN, MEALIE_URL

# `_image_mime` prüft JPEG/PNG/WebP anhand der Magic Bytes und ist damit genau die
# Auskunft, die der Upload für die Dateiendung braucht. Bewusst wiederverwendet statt
# hier ein zweites Mal definiert: welche Bildtypen der Dienst kennt, soll an einer
# Stelle stehen. `naming.py` greift aus demselben Grund auf `llm._post` zu.
from llm import _image_mime

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 60

# Live geprüft am 2026-08-22, siehe Moduldocstring. Nicht in DESIGN.md §3 vorgesehen,
# deshalb keine eigene Umgebungsvariable - dieses Homelab betreibt genau eine Gruppe.
GROUP_SLUG = "home"

_HEADERS = {
    "Authorization": f"Bearer {MEALIE_TOKEN}",
    "Content-Type": "application/json",
}


# Live verifiziert am 2026-08-24, siehe Moduldocstring: der Wortlaut, mit dem Mealie
# ein Platzhalter-Rezept füllt, wenn create/url auf der Seite keine Zutaten bzw. keine
# Schritte findet. Vergleich klein geschrieben, damit eine künftige Gross-/
# Kleinschreibungsänderung bei Mealie die Erkennung nicht stillschweigend abschaltet.
_PLACEHOLDER_INGREDIENT_NOTE = "could not detect ingredients"
_PLACEHOLDER_INSTRUCTION_TEXT = "could not detect instructions"


class MealieError(Exception):
    """Mealie hat einen Schreibvorgang abgelehnt, der laut Ablauf gelingen muss."""


class MealieUnavailableError(MealieError):
    """Mealie war gar nicht erreichbar - Verbindungsfehler, Namensauflösung oder
    Zeitüberschreitung, also kein HTTP-Status.

    Eigene Klasse, weil die beiden Fälle verschieden ausgehen (A20, design.md): "Mealie
    hat nein gesagt" ist ein endgültiger Fehlschlag, "Mealie war nicht da" ist ein
    "später nochmal" und wird geparkt. Unterklasse von `MealieError`, damit jeder
    bestehende `except MealieError` unverändert weiter greift."""


def _slugify(tag: str) -> str:
    """Mealies RecipeTag verlangt Name und Slug getrennt. Für die eine feste
    Kennzeichnung "auto-import" aus dem Ablauf (§5, Schritt 8) reicht diese einfache
    Umformung; keine allgemeine Slug-Bibliothek für einen einzigen bekannten Wert."""
    return tag.strip().lower().replace(" ", "-")


def import_url(url: str) -> str | None:
    """POST /api/recipes/create/url. Gibt bei jedem Fehlschlag None zurück und wirft
    nicht - Stufe 1 des Ablaufs (§5, Schritt 5a) darf regulär danebengehen, wenn
    Mealie die Seite nicht selbst scrapen kann."""
    log.info("POST %s/api/recipes/create/url url=%s", MEALIE_URL, url)
    try:
        resp = requests.post(
            f"{MEALIE_URL}/api/recipes/create/url",
            headers=_HEADERS,
            json={"url": url},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.warning("import_url(%s) fehlgeschlagen: %s", url, exc)
        return None

    if resp.status_code != 201:
        log.info(
            "import_url(%s) -> %d, Mealie kann die Seite nicht scrapen: %s",
            url, resp.status_code, resp.text[:300],
        )
        return None

    slug = resp.json()
    log.info("import_url(%s) -> slug=%s", url, slug)
    return slug


def create_from_jsonld(data: dict) -> str:
    """POST /api/recipes/create/html-or-json mit dem JSON-LD-Dokument aus
    schema.to_jsonld() als String im Feld "data". Dies ist der Rückfall aus Ablauf
    Schritt 7 und muss gelingen; ein Fehlschlag ist ein MealieError."""
    log.info("POST %s/api/recipes/create/html-or-json", MEALIE_URL)
    try:
        resp = requests.post(
            f"{MEALIE_URL}/api/recipes/create/html-or-json",
            headers=_HEADERS,
            json={"data": json.dumps(data)},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise MealieUnavailableError(str(exc)) from exc

    if resp.status_code != 201:
        raise MealieError(f"{resp.status_code}: {resp.text[:300]}")

    slug = resp.json()
    log.info("create_from_jsonld -> slug=%s", slug)
    return slug


def _existing_tag_ids() -> dict[str, str]:
    """slug -> id aller vorhandenen Tags. Live nachgemessen am 2026-08-22: PATCH
    /api/recipes/{slug} legt ein Tag ohne "id" nicht wieder auffindbar an, sondern
    lehnt mit der irreführenden Meldung "Recipe already exists" ab, sobald ein Tag
    mit demselben Slug schon existiert. Deshalb vorher nachschlagen und, wenn
    vorhanden, die "id" mitschicken statt nur name/slug."""
    try:
        resp = requests.get(
            f"{MEALIE_URL}/api/organizers/tags",
            headers=_HEADERS,
            params={"perPage": -1},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return {item["slug"]: item["id"] for item in resp.json().get("items", [])}
    except requests.RequestException as exc:
        log.warning("Tag-Liste konnte nicht geladen werden, lege Tags ggf. doppelt an: %s", exc)
        return {}


def set_tags(slug: str, tags: list[str]) -> None:
    """PATCH /api/recipes/{slug} mit dem Tags-Feld. Legt fehlende Tags neu an, siehe
    Moduldocstring zur Wahl gegenüber /api/recipes/bulk-actions/tag."""
    existing = _existing_tag_ids()
    tag_bodies = []
    for tag in tags:
        tag_slug = _slugify(tag)
        entry = {"name": tag, "slug": tag_slug}
        if tag_slug in existing:
            entry["id"] = existing[tag_slug]
        tag_bodies.append(entry)
    body = {"tags": tag_bodies}
    log.info("PATCH %s/api/recipes/%s tags=%s", MEALIE_URL, slug, tags)
    try:
        resp = requests.patch(
            f"{MEALIE_URL}/api/recipes/{slug}",
            headers=_HEADERS,
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise MealieUnavailableError(str(exc)) from exc

    if resp.status_code != 200:
        raise MealieError(f"{resp.status_code}: {resp.text[:300]}")


def get_recipe(slug: str) -> dict:
    """GET /api/recipes/{slug}, das volle `Recipe-Output`-Objekt. Grundlage für
    `get_recipe_name` (Review-A8-Befund 2) und `is_placeholder` (A16). Live gegen
    `mealie.local:30081/openapi.json` verifiziert am 2026-08-23: der Pfad existiert mit
    Methode GET, ein Aufruf ohne Token endet mit 401 - Auth ist also wie bei den übrigen
    Aufrufen Pflicht."""
    log.info("GET %s/api/recipes/%s", MEALIE_URL, slug)
    try:
        resp = requests.get(
            f"{MEALIE_URL}/api/recipes/{slug}",
            headers=_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise MealieUnavailableError(str(exc)) from exc

    if resp.status_code != 200:
        raise MealieError(f"{resp.status_code}: {resp.text[:300]}")

    return resp.json()


def get_recipe_name(slug: str) -> str:
    """Auf dem Weg, den Mealie selbst geschabt hat (Ablauf §5, Schritt 5a), liefert
    `import_url` nur den Slug, kein `Recipe`-Objekt - die Rückmeldung braucht aber den
    Namen, nicht den Slug (DESIGN.md §7)."""
    return get_recipe(slug)["name"]


def is_placeholder(recipe: dict) -> bool:
    """True, wenn `recipe` (Antwort von `get_recipe`) weder Zutaten noch
    Zubereitungsschritte trägt (A16, Befund aus A10: 2026-08-23).

    Leere Listen kommen von Mealie dabei nicht vor - live verifiziert am 2026-08-24,
    siehe Moduldocstring: `create/url` füllt stattdessen genau einen Sentinel-Eintrag je
    Liste. Deshalb zählt ein Eintrag ohne brauchbaren Text ebenso als leer wie eine
    tatsächlich leere Liste, und `all()` auf einer leeren Liste ist `True` - beide Fälle
    landen damit auf demselben Zweig."""

    def _blank_or_sentinel(text: str | None, sentinel: str) -> bool:
        value = (text or "").strip().lower()
        return value == "" or value == sentinel

    ingredients = recipe.get("recipeIngredient") or []
    instructions = recipe.get("recipeInstructions") or []
    no_ingredients = all(
        _blank_or_sentinel(item.get("note") or item.get("display"), _PLACEHOLDER_INGREDIENT_NOTE)
        for item in ingredients
    )
    no_instructions = all(
        _blank_or_sentinel(item.get("text"), _PLACEHOLDER_INSTRUCTION_TEXT) for item in instructions
    )
    return no_ingredients and no_instructions


def delete_recipe(slug: str) -> None:
    """DELETE /api/recipes/{slug}. Einziger Aufrufer ist der Platzhalter-Zweig in
    `app.py` (A16): der Slug stammt dort immer aus der eigenen `import_url`-Antwort
    desselben Laufs, nie aus einer fremden Quelle. Wirft `MealieError` bei Fehlschlag;
    der Aufrufer entscheidet, ob das den Import stoppt (siehe app.py, dort bewusst
    nicht fatal - ein liegen gebliebener Platzhalter ist ein kosmetischer Fehler, kein
    gescheiterter Import)."""
    log.info("DELETE %s/api/recipes/%s", MEALIE_URL, slug)
    try:
        resp = requests.delete(
            f"{MEALIE_URL}/api/recipes/{slug}",
            headers=_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise MealieUnavailableError(str(exc)) from exc

    if resp.status_code not in (200, 204):
        raise MealieError(f"{resp.status_code}: {resp.text[:300]}")


def recipe_exists(slug: str) -> bool:
    """True, solange Mealie unter diesem Slug noch ein Rezept führt.

    Grundlage der Prüfung in `app._notify_if_done`: ein `done`-Eintrag im Store bleibt
    stehen, auch wenn das Rezept danach in Mealie gelöscht wird. Ohne diese Abfrage
    blockiert der alte Eintrag jeden weiteren Anlauf derselben Quelle und die
    Rückmeldung trägt einen Link auf ein Rezept, das es nicht mehr gibt.

    Nur eine `404` gilt als "weg". Jeder andere Fehlschlag - Netz, Timeout, 5xx -
    liefert True: ein gerade nicht erreichbares Mealie darf keinen zweiten Import
    derselben Quelle auslösen."""
    try:
        resp = requests.get(
            f"{MEALIE_URL}/api/recipes/{slug}",
            headers=_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.warning("Mealie nicht erreichbar bei der Prüfung von %s: %s", slug, exc)
        return True

    if resp.status_code == 404:
        return False
    if resp.status_code != 200:
        log.warning("Unerwartete Antwort bei der Prüfung von %s: %d", slug, resp.status_code)
    return True


def recipe_link(slug: str) -> str:
    """Klickbare Oberflächen-URL, Form live verifiziert - siehe Moduldocstring."""
    return f"{MEALIE_URL}/g/{GROUP_SLUG}/r/{slug}"


def rename(slug: str, name: str) -> str:
    """PATCH /api/recipes/{slug} mit nur dem Namensfeld (Feature A18). Gibt den danach
    gueltigen Slug zurueck.

    Derselbe Endpunkt und dasselbe `Recipe-Input`-Schema wie `set_tags`, das ohne
    Pflichtfelder auskommt - ein PATCH mit nur "name" aendert daher nichts sonst.

    **Mealie leitet den Slug bei einer Umbenennung neu ab.** Live verifiziert am
    2026-08-24 gegen Mealie v3.22.0 auf mealie.local:30081: nach dem PATCH auf
    `apfel-kasekuchen-vom-blech-von-barchenknutscher` antwortet der alte Slug mit 404 und
    das Rezept liegt unter `apfel-kasekuchen-vom-blech`. Ein im Rumpf mitgeschickter
    `slug` wird dabei ignoriert - eine Probe mit unveraendertem `slug` und neuem `name`
    ergab wieder den aus dem Namen abgeleiteten Slug. `recipe-naming-plan.md` §2.1 nimmt
    das Gegenteil an; deshalb steht der neue Slug in der Rueckgabe, und der Aufrufer
    fuehrt Link, store-Eintrag und Tags darauf nach. Unschaedlich ist das, weil die
    Umbenennung vor `store.finish()` und vor der Push-Meldung laeuft: ausserhalb des
    Dienstes kennt zu diesem Zeitpunkt noch niemand den alten Slug.
    """
    log.info("PATCH %s/api/recipes/%s name=%r", MEALIE_URL, slug, name)
    try:
        resp = requests.patch(
            f"{MEALIE_URL}/api/recipes/{slug}",
            headers=_HEADERS,
            json={"name": name},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise MealieUnavailableError(str(exc)) from exc

    if resp.status_code != 200:
        raise MealieError(f"{resp.status_code}: {resp.text[:300]}")

    try:
        new_slug = resp.json().get("slug")
    except ValueError:
        new_slug = None
    if not new_slug:
        # Sollte nicht vorkommen (die Antwort ist das aktualisierte Rezept), waere aber
        # kein Grund, einen schon geschriebenen Namen zurueckzurollen.
        log.warning("PATCH-Antwort fuer %s enthielt keinen Slug, rechne weiter mit dem alten", slug)
        return slug
    if new_slug != slug:
        log.info("Mealie hat den Slug bei der Umbenennung geaendert: %s -> %s", slug, new_slug)
    return new_slug


def recipe_texts(recipe: dict) -> tuple[list[str], list[str]]:
    """Zutaten und Zubereitungsschritte einer `get_recipe`-Antwort als Freitextzeilen.

    Mealie liefert beide Listen als Objekte (`note`/`display` bzw. `text`), die
    Namensstufe erwartet dagegen Freitext wie `Recipe` aus DESIGN.md §4. Die Umformung
    steht hier und nicht in `naming.py`, weil sie Wissen ueber Mealies Antwortform ist -
    dasselbe Wissen, auf dem schon `is_placeholder` aufsetzt.
    """
    ingredients = [
        str(item.get("note") or item.get("display") or "").strip()
        for item in (recipe.get("recipeIngredient") or [])
    ]
    instructions = [
        str(item.get("text") or "").strip() for item in (recipe.get("recipeInstructions") or [])
    ]
    return [i for i in ingredients if i], [s for s in instructions if s]


# Endung je Bildtyp, den `_image_mime` kennt. Mealie verlangt sie getrennt vom Dateinamen
# im Feld `extension` (siehe Moduldocstring).
_IMAGE_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

# Die Bilddatei, an der "hat ein Bild" gemessen wird. Mealie legt zu jedem Bild drei
# Grössen ab (original, min-original, tiny-original); die Probe vom 2026-09-20 fand sie
# stets gemeinsam vorhanden oder gemeinsam abwesend, deshalb reicht eine davon.
_IMAGE_RENDITION = "original.webp"


def has_image(recipe: dict) -> bool:
    """True, wenn zu `recipe` (Antwort von `get_recipe`) in Mealie wirklich eine
    Bilddatei liegt (A19).

    **Nicht** am Feld `image` entschieden: das trägt eine Kennung wie `'QziM'` auch bei
    einem Rezept ganz ohne Bild - gemessen am 2026-09-20 an 28 von 29 bildlosen
    Rezepten, siehe Moduldocstring. Gefragt wird deshalb die Mediendatei selbst, die
    einzige Auskunft, die sich dabei als belastbar erwiesen hat.

    Grundlage der Entscheidung in `app._attach_image`: ein Bild, das Mealie selbst von
    der Quellseite geholt hat, zeigt das echte Gericht und wird nie durch ein erzeugtes
    ersetzt. Im Zweifel gilt deshalb "hat ein Bild": eine fehlende id, ein Netzfehler
    oder ein unerwarteter Status sind kein Grund, ein vorhandenes Foto zu überschreiben.
    Nur eine glatte `404` heisst "kein Bild"."""
    recipe_id = recipe.get("id")
    if not recipe_id:
        log.warning("Rezept ohne id, Bildstand unbekannt - gilt als 'hat ein Bild'")
        return True

    url = f"{MEALIE_URL}/api/media/recipes/{recipe_id}/images/{_IMAGE_RENDITION}"
    log.info("GET %s", url)
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {MEALIE_TOKEN}"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.warning("Bildstand von %s nicht abfragbar (%s) - gilt als 'hat ein Bild'", recipe_id, exc)
        return True

    if resp.status_code == 404:
        return False
    if resp.status_code != 200:
        log.warning(
            "Unerwartete Antwort %d beim Bildstand von %s - gilt als 'hat ein Bild'",
            resp.status_code, recipe_id,
        )
    return True


def set_image(slug: str, data: bytes) -> None:
    """Hängt `data` als Rezeptbild an `slug` (A19). Wirft `MealieError` bei Fehlschlag.

    Einziger Aufrufer ist die Bildstufe in `app.py`, die jeden Fehlschlag auffängt: ein
    Rezept ohne Bild ist ein kosmetischer Mangel, kein gescheiterter Import. Form des
    Aufrufs und der Grund für die fehlenden `_HEADERS` stehen im Moduldocstring."""
    try:
        mime = _image_mime(data)
        extension = _IMAGE_EXTENSIONS[mime]
    except Exception as exc:
        raise MealieError(f"unbekanntes Bildformat: {exc}") from exc

    log.info("PUT %s/api/recipes/%s/image (%d Bytes, %s)", MEALIE_URL, slug, len(data), extension)
    try:
        resp = requests.put(
            f"{MEALIE_URL}/api/recipes/{slug}/image",
            headers={"Authorization": f"Bearer {MEALIE_TOKEN}"},
            files={"image": (f"image.{extension}", data, mime)},
            data={"extension": extension},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise MealieUnavailableError(str(exc)) from exc

    if resp.status_code not in (200, 201):
        raise MealieError(f"{resp.status_code}: {resp.text[:300]}")
