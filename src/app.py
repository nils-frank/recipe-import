"""HTTP-Einstiegspunkt für recipe-import, siehe DESIGN.md §5 (Ablauf) und §6
(`src/app.py (A4)`, Uploadweg A12).

`POST /import` und `POST /import/file` antworten sofort `202` und verarbeiten im Hintergrund über
`BackgroundTasks` - bewusst keine Queue, siehe DESIGN.md §5/§10 (Nicht-Ziele Phase 1).
Bei jedem Fehlschlag landet ein Grund in `store` und beim Nutzer per `ha_notify`, nie
ein stiller Abbruch (DESIGN.md §5, §7).

Offene Frage zu den Ausnahmeklassen `SourceError`, `NoTranscriptError` (A1) und
`LlmError` (A2): DESIGN.md §6 nennt nur die Namen, nicht das Modul, in dem sie definiert
sind, und A1/A4 laufen gleichzeitig, ohne sich gegenseitig zu lesen. Statt einen
Importpfad zu raten, der beim Zusammenführen der Zweige bricht, wird hier nach dem
Klassennamen der aufgefangenen Ausnahme unterschieden - siehe `_describe_source_error`.

`POST /import/file` (A12) trägt als einziger Endpunkt eine eigene Authentifizierung: er
wird vom Kurzbefehl direkt aufgerufen, weil ein Home-Assistant-Webhook JSON und
Formularfelder annimmt, aber keine Binärdatei sinnvoll weiterreicht (DESIGN.md §6).
Der Kurzbefehl wertet die HTTP-Antwort nicht aus (DESIGN.md §9, Punkt 6). Deshalb wird
hier nur abgewiesen, was ohne Lesen der Datei feststeht - Token, Anzahl, Grösse,
Ratenbegrenzung. Alles, was erst beim Lesen auffällt (HEIC, gescanntes PDF, fremder
Typ), läuft bewusst über den Hintergrundweg und damit über `ha_notify`: eine `400`, die
niemand sieht, wäre der stille Abbruch, den DESIGN.md §5 verbietet.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, Request, UploadFile

import config
import ha_notify
import mealie_client
import naming
import store
from classify import classify, normalize_url, url_hash
from llm import extract_recipe, extract_recipe_from_images
from schema import to_jsonld
from sources import document, site, youtube

# Präfix der `url`-Spalte für Uploads, damit Datei- und URL-Importe in derselben Tabelle
# unterscheidbar bleiben (DESIGN.md §5: "datei:<name>").
FILE_URL_PREFIX = "datei:"

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))
log = logging.getLogger(__name__)

# Starke Referenzen auf die beim Start wiederaufgenommenen Importe. asyncio hält eine
# laufende Aufgabe nur schwach: ohne diese Menge kann der Sammler eine Wiederaufnahme
# mitten im Lauf einsammeln, und der unterbrochene Import bliebe für immer "pending",
# ohne dass irgendwo ein Fehler auftaucht. Der Callback räumt den Eintrag wieder ab.
_resume_tasks: set[asyncio.Task] = set()


def _track(task: asyncio.Task) -> None:
    _resume_tasks.add(task)
    task.add_done_callback(_resume_tasks.discard)


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
    resumed = store.pending()
    for entry in resumed:
        if entry["url"].startswith(FILE_URL_PREFIX):
            # Bei einem Datei-Import liegen nur die Bytes des Aufrufs vor, und die sind
            # nach einem Neustart weg - gespeichert wird bewusst nur ihr Hash (DESIGN.md
            # §5). Statt endlos "pending" zu bleiben, wird der Eintrag als gescheitert
            # markiert und der Mensch gebeten, die Datei erneut zu teilen.
            message = "Der Neustart kam mitten in der Verarbeitung. Bitte die Datei noch einmal teilen."
            log.warning("Datei-Import %s nicht wiederaufnehmbar: %s", entry["url_hash"], message)
            store.fail(entry["url_hash"], message)
            ha_notify.notify("Import fehlgeschlagen", message)
            continue
        log.info("Nehme unterbrochenen Import wieder auf: %s", entry["url"])
        # Direkt _run_import statt _process_import: die Zeile kommt aus store.pending()
        # und ist damit bereits exklusiv "pending" - dieser Container ist beim Start ihr
        # einziger Besitzer. _process_import würde über store.start() den atomaren Claim
        # aus Befund 3 (Review A8) erneut versuchen, der auf einem schon pending-
        # Eintrag scheitert (die WHERE-Klausel lässt nur `failed` -> `pending` zu) und
        # die Wiederaufnahme verhindern würde.
        _track(asyncio.create_task(_run_import(entry["url_hash"], entry["url"])))
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/import", status_code=202)
async def import_recipe(request: Request, background_tasks: BackgroundTasks) -> dict:
    try:
        body = await request.json()
    except Exception as exc:
        # Der Antworttext bleibt die kurze deutsche Meldung - der Aufrufer ist der
        # Kurzbefehl, dem ein Parserfehler nichts sagt. `from exc` hängt die Ursache
        # aber an die Ausnahme, damit sie im Serverprotokoll auftaucht statt zu
        # verschwinden.
        raise HTTPException(status_code=400, detail="Kein gültiges JSON im Anfragetext") from exc

    url = (body or {}).get("url") if isinstance(body, dict) else None
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(status_code=400, detail="URL fehlt")

    normalized = normalize_url(url.strip())
    if classify(normalized) == "unsupported":
        raise HTTPException(status_code=400, detail="URL wird nicht unterstützt")

    _enforce_rate_limit(normalized)

    background_tasks.add_task(_process_import, normalized)
    return {"status": "accepted"}


def _enforce_rate_limit(subject: str) -> None:
    """Ein gemeinsamer Zähler für beide Endpunkte (DESIGN.md §11): sie lösen dieselben
    LLM-Kosten aus, Bilder sogar höhere."""
    if store.recent_count(3600) >= config.RATE_LIMIT_PER_HOUR:
        log.warning("Ratenbegrenzung erreicht (%d/h), lehne %s ab", config.RATE_LIMIT_PER_HOUR, subject)
        raise HTTPException(status_code=429, detail="Ratenbegrenzung erreicht")


@app.post("/import/file", status_code=202)
async def import_file(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    authorization: str | None = Header(default=None),
) -> dict:
    # hmac.compare_digest statt "==", damit die Laufzeit des Vergleichs nichts über den
    # Token verrät. Weder der erwartete noch der gelieferte Wert geht je in eine
    # Protokollzeile.
    supplied = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    if not supplied or not hmac.compare_digest(supplied, config.IMPORT_TOKEN):
        log.warning("POST /import/file ohne gültigen Token abgewiesen")
        raise HTTPException(status_code=401, detail="Token fehlt oder ist falsch")

    if not files:
        raise HTTPException(status_code=400, detail="Keine Datei im Anfragetext")
    if len(files) > config.MAX_UPLOAD_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Höchstens {config.MAX_UPLOAD_FILES} Dateien je Anfrage",
        )

    # Erst die angekündigten Grössen prüfen, dann lesen: der Container läuft mit
    # mem_limit 512m (DESIGN.md §10), ein zu grosser Upload soll eine 413 ergeben und
    # keinen Speicherfehler.
    limit_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
    declared = sum(f.size or 0 for f in files)
    if declared > limit_bytes:
        raise HTTPException(status_code=413, detail=f"Mehr als {config.MAX_UPLOAD_MB} MB je Anfrage")

    uploads: list[document.Upload] = []
    total = 0
    for item in files:
        data = await item.read()
        total += len(data)
        if total > limit_bytes:
            raise HTTPException(status_code=413, detail=f"Mehr als {config.MAX_UPLOAD_MB} MB je Anfrage")
        uploads.append(
            document.Upload(
                filename=item.filename or "unbenannt",
                content_type=item.content_type or "",
                data=data,
            )
        )

    if not any(u.data for u in uploads):
        raise HTTPException(status_code=400, detail="Die Datei war leer")

    _enforce_rate_limit(f"{FILE_URL_PREFIX}{uploads[0].filename}")

    background_tasks.add_task(_process_file, uploads)
    return {"status": "accepted"}


def _describe_source_error(exc: Exception) -> str:
    """Bildet eine Ausnahme aus der Extraktionsstufe (A1 site/youtube, A2 llm) auf den
    wörtlichen Rückmeldungstext aus DESIGN.md §7 ab. Dispatch über den Klassennamen statt
    über einen Import, siehe Moduldocstring."""
    kind = type(exc).__name__
    if kind == "NoTranscriptError":
        return "Das Video hat keine Untertitel, daraus lässt sich kein Rezept lesen."
    if kind == "ThrottledError":
        # A15, DESIGN.md §7: eine YouTube-Drosselung (HTTP 429) ist kein "kein Rezept
        # gefunden" - sonst sieht ein Nutzer keinen Grund, es später erneut zu
        # versuchen.
        return "YouTube drosselt gerade die Untertitel. Bitte später erneut teilen."
    if kind == "SourceError":
        return "Auf der Seite war kein Rezept zu finden."
    if kind == "UnreadablePdfError":
        return "Dieses PDF enthält keinen lesbaren Text. Ein Foto der Seite funktioniert besser."
    if kind == "UnsupportedFileError":
        return f"Diese Datei kann ich nicht lesen: {exc}"
    if kind == "NoRecipeFoundError":
        # Die Klasse trägt den zur Quelle passenden Wortlaut aus DESIGN.md §7 schon in
        # sich ("Auf dem Bild war kein Rezept zu erkennen."), deshalb wörtlich.
        return str(exc)
    if kind == "LlmOverloadedError":
        # Wie ThrottledError oben (A15): der Anbieter selbst sagt "später nochmal",
        # kein "kein Rezept gefunden" - sonst sieht der Nutzer keinen Grund für einen
        # erneuten Versuch.
        return "Das Sprachmodell ist gerade überlastet. Bitte später erneut teilen."
    if kind == "LlmError":
        return f"Die Rezepterkennung ist gescheitert: {exc}"
    return f"Import fehlgeschlagen: {exc}"


async def _process_import(url: str) -> None:
    normalized = normalize_url(url)
    h = url_hash(normalized)

    if _notify_if_done(h):
        return

    if not store.start(h, normalized):
        # Befund 3 (Review A8): eine fast gleichzeitige zweite Anfrage zur selben URL
        # kam hier an, während die erste den Eintrag schon "pending" hält - store.start()
        # hat das atomar erkannt und nichts geändert. Die erste Anfrage läuft die Kette
        # zu Ende und benachrichtigt; ein zweiter Anlauf wäre ein doppelter Mealie-Aufruf.
        log.info("Import %s läuft bereits, überspringe doppelten Anlauf", h)
        return

    await _run_import(h, normalized)


def _reject_if_placeholder(slug: str, normalized: str) -> tuple[str | None, dict | None]:
    """A16, Befund aus A10 (2026-08-23): `mealie_client.import_url()` liefert für eine
    erreichbare Seite ohne Rezept kein `None`, sondern legt selbst ein Platzhalter-
    Rezept an (Titel der Seite, Sentinel-Zutat, Sentinel-Schritt - live verifiziert am
    2026-08-24, siehe mealie_client.py). Ein Platzhalter zählt nicht als Erfolg der
    Stufe: er wird gelöscht, `_run_import` fällt danach auf `site.fetch()` zurück, genau
    wie beim regulären `None` aus `import_url()`.

    Gibt `(None, None)` zurück, wenn es sich um einen Platzhalter handelte (und ihn dann
    gelöscht hat oder das zumindest versucht hat), sonst `(slug, recipe_data)`. Kann die
    Platzhalter-Prüfung selbst nicht durchgeführt werden (Mealie nicht erreichbar), wird
    das nur protokolliert - der ursprüngliche `slug` gilt dann als Erfolg, wie vor A16,
    und `recipe_data` bleibt `None`.

    Die abgefragte Antwort wird mit zurückgegeben, statt sie zu verwerfen: die Namensstufe
    (A18) braucht denselben Datensatz, und ein zweiter `get_recipe`-Aufruf für dieselbe
    Information wäre eine verdoppelte Anfrage (recipe-naming-plan.md §2.1)."""
    try:
        recipe_data = mealie_client.get_recipe(slug)
    except Exception as exc:  # noqa: BLE001 - Platzhalter-Pruefung darf den Import nie stoppen
        log.warning("Platzhalter-Prüfung für %s (%s) nicht möglich, werte als Erfolg: %s", slug, normalized, exc)
        return slug, None

    if not mealie_client.is_placeholder(recipe_data):
        return slug, recipe_data

    log.info("import_url(%s) hat nur ein Platzhalter-Rezept angelegt (slug=%s), lösche und versuche die nächste Stufe", normalized, slug)
    try:
        mealie_client.delete_recipe(slug)
    except Exception as exc:  # noqa: BLE001 - Aufraeumen darf den Import nicht scheitern lassen
        # Nicht fatal (DESIGN.md §5, kein stiller Abbruch gilt für den Import, nicht für
        # diese Aufräumarbeit): ein liegen gebliebener Platzhalter ist kosmetisch, kein
        # Grund, den ganzen Import scheitern zu lassen.
        log.warning("Platzhalter %s konnte nicht gelöscht werden: %s", slug, exc)
    return None, None


async def _run_import(h: str, normalized: str) -> None:
    kind = classify(normalized)
    log.info("Starte Import %s als %s (%s)", h, kind, normalized)

    try:
        recipe = None
        slug = None
        mealie_data = None

        if kind == "site":
            slug = mealie_client.import_url(normalized)
            if slug is not None:
                slug, mealie_data = _reject_if_placeholder(slug, normalized)
            if slug is None:
                result = site.fetch(normalized)
                recipe = result.recipe if result.recipe is not None else extract_recipe(result.text, normalized)
        elif kind == "youtube":
            result = youtube.fetch(normalized)
            recipe = extract_recipe(result.text, normalized)
        else:
            # Wird bereits am HTTP-Eingang abgewiesen (400), siehe import_recipe().
            # Diese Verzweigung ist eine Absicherung für den Wiederaufnahme-Pfad beim
            # Start, der ohne erneute HTTP-Validierung läuft.
            raise ValueError(f"nicht unterstützte URL: {normalized}")
    except Exception as exc:  # noqa: BLE001 - jeder Fehlschlag wird gemeldet, nie still verworfen
        message = _describe_source_error(exc)
        log.error("Extraktion fehlgeschlagen für %s: %s", h, message)
        store.fail(h, message)
        ha_notify.notify("Import fehlgeschlagen", message)
        return

    _publish(h, recipe, slug, normalized, mealie_data)


def _notify_if_done(h: str) -> bool:
    """True, wenn zu diesem Hash bereits ein fertiges Rezept existiert - dann geht nur
    der alte Link heraus (DESIGN.md §5, Schritt 2). Gilt für URLs wie für Dateien, weil
    `content_hash` denselben Hash-Raum benutzt."""
    existing = store.find(h)
    if not existing or existing["status"] != "done":
        return False
    link = mealie_client.recipe_link(existing["slug"]) if existing.get("slug") else None
    log.info("Quelle bereits importiert (%s), sende alten Link", h)
    ha_notify.notify(
        "Rezept schon vorhanden",
        f"{existing['title']} wurde bereits importiert",
        link,
    )
    return True


def _rename_scraped(slug: str, source: str, mealie_data: dict | None) -> tuple[str | None, str]:
    """Namensstufe auf dem Weg, den Mealie selbst geschabt hat (A18, Plan §2.1, Punkt 2).

    Gibt `(neuer Name, gültiger Slug)` zurück; der Name ist `None`, wenn nichts
    geschrieben wurde. Der Slug wird mit zurückgegeben, weil Mealie ihn bei einer
    Umbenennung neu ableitet - live verifiziert am 2026-08-24, siehe
    `mealie_client.rename`. Ab hier gilt nur noch der zurückgegebene Slug: für
    `store.finish`, für `set_tags` und für den Link in der Push-Meldung.

    Wirft nie: die Namensstufe darf einen bereits angelegten Import nicht nachträglich
    scheitern lassen (Plan §2.2)."""
    if mealie_data is None:
        # Nur wenn schon die Platzhalter-Prüfung Mealie nicht erreicht hat. Dann fehlen
        # Zutaten und Schritte, und ein Name allein aus dem Seitentitel wäre geraten.
        log.warning("Kein Rezeptinhalt aus Mealie für %s, überspringe die Namensstufe", slug)
        return None, slug

    ingredients, instructions = mealie_client.recipe_texts(mealie_data)
    new_name = naming.make_name(mealie_data.get("name"), source, ingredients, instructions)
    if new_name is None:
        return None, slug

    try:
        return new_name, mealie_client.rename(slug, new_name)
    except Exception as exc:  # noqa: BLE001 - Namensstufe darf den Import nie scheitern lassen
        log.warning("Umbenennen von %s auf %r fehlgeschlagen, Name bleibt: %s", slug, new_name, exc)
        return None, slug


def _publish(h, recipe, slug: str | None, source: str, mealie_data: dict | None = None) -> None:
    """Schritte 7 bis 10 des Ablaufs (DESIGN.md §5), gemeinsam für beide Endpunkte.

    Reparatur zu Befund 1 (Review A8, 2026-08-23): sobald Mealie einen Slug geliefert
    hat, existiert das Rezept dort bereits - store.finish() läuft deshalb sofort danach,
    noch bevor set_tags() versucht wird. Scheitert set_tags() trotzdem, ist das nur noch
    ein fehlendes Tag, kein gescheiterter Import: der Eintrag bleibt "done", store.fail()
    wird nicht mehr aufgerufen. Vorher landete ein Fehlschlag von set_tags() im
    Fehlerpfad, markierte den Eintrag "failed" und liess einen erneuten Anlauf (erneut
    geteilt oder nach einem Neustart über store.pending()) das längst angelegte Rezept
    ein zweites Mal anlegen.

    Reparatur zu Befund 2 (Review A8, 2026-08-23): auf dem Weg, den Mealie selbst
    geschabt hat (`kind == "site"`, slug schon gesetzt, `recipe` bleibt None), kam der
    Name bisher aus dem Slug selbst. DESIGN.md §7 verlangt den Rezeptnamen; der wird jetzt
    über mealie_client.get_recipe_name() aus Mealies eigener Antwort geholt statt aus
    dem Slug zurückgerechnet.

    Feature A18 (recipe-naming-plan.md): dazwischen liegt jetzt die Namensstufe, an genau
    dieser einen Stelle und damit für alle vier Wege. Liegt ein `Recipe` vor, wird der
    Name **vor** `create_from_jsonld` ersetzt - Mealie bildet den Slug dann gleich aus
    dem guten Namen. Hat Mealie selbst geschabt, existiert das Rezept schon und der Name
    geht per `rename()` nach; der Slug bleibt dabei bewusst stehen, damit der gleich
    versendete Link gültig bleibt."""
    try:
        if slug is None:
            new_name = naming.make_name(
                recipe.name, source, list(recipe.recipeIngredient), list(recipe.recipeInstructions)
            )
            if new_name is not None:
                recipe = recipe.model_copy(update={"name": new_name})
            slug = mealie_client.create_from_jsonld(to_jsonld(recipe))
            title = recipe.name
        else:
            renamed, slug = _rename_scraped(slug, source, mealie_data)
            # get_recipe_name() nur, wenn die Platzhalter-Prüfung Mealie nicht erreicht
            # hat - sonst steht der Name schon in `mealie_data` und ein zweiter GET
            # wäre dieselbe Anfrage ein zweites Mal.
            title = renamed or (mealie_data or {}).get("name") or mealie_client.get_recipe_name(slug)
    except Exception as exc:  # noqa: BLE001 - jeder Fehlschlag wird gemeldet, nie still verworfen
        message = f"Mealie hat den Import abgelehnt: {exc}"
        log.error("Mealie-Import fehlgeschlagen für %s: %s", h, message)
        store.fail(h, message)
        ha_notify.notify("Import fehlgeschlagen", message)
        return

    # Ab hier ist das Rezept in Mealie angelegt: sofort persistieren, damit ein
    # gleich folgender set_tags()-Fehlschlag keine Doppelanlage mehr auslösen kann.
    store.finish(h, slug, title)

    try:
        mealie_client.set_tags(slug, ["auto-import"])
    except Exception as exc:  # noqa: BLE001 - fehlendes Tag ist kosmetisch, kein Abbruchgrund
        log.warning("set_tags(%s) fehlgeschlagen, Rezept bleibt ohne Tag: %s", slug, exc)

    link = mealie_client.recipe_link(slug)
    log.info("Import %s abgeschlossen: slug=%s", h, slug)
    ha_notify.notify("Rezept angelegt", title, link)


async def _process_file(uploads: list[document.Upload]) -> None:
    """Ablauf für `POST /import/file` (DESIGN.md §5). Unterschied zum URL-Weg: der Hash
    kommt aus den Bytes, und Mealies eigener Scraper hat hier nichts zu tun - eine Datei
    kann er nicht abrufen."""
    h = document.content_hash(uploads)
    label = f"{FILE_URL_PREFIX}{uploads[0].filename}"

    if _notify_if_done(h):
        return

    if not store.start(h, label):
        # Befund 3 (Review A8), siehe _process_import: eine zweimal geteilte Datei
        # kurz hintereinander soll keinen zweiten Mealie-Aufruf auslösen.
        log.info("Datei-Import %s läuft bereits, überspringe doppelten Anlauf", h)
        return

    log.info("Starte Datei-Import %s (%d Datei(en), %s)", h, len(uploads), label)

    try:
        result = document.read(uploads)
        if result.images:
            recipe = extract_recipe_from_images(list(result.images), label)
            # Für die Namensstufe zählt die Art der Quelle, nicht der Dateiname: das
            # Modell soll wissen, dass der vorhandene Name von einem Foto stammt und
            # dort fehlen darf (recipe-naming-plan.md §3.2).
            source = "Foto"
        else:
            recipe = extract_recipe(result.text, label)
            source = "PDF"
    except Exception as exc:  # noqa: BLE001 - jeder Fehlschlag wird gemeldet, nie still verworfen
        message = _describe_source_error(exc)
        log.error("Datei-Import fehlgeschlagen für %s: %s", h, message)
        store.fail(h, message)
        ha_notify.notify("Import fehlgeschlagen", message)
        return

    _publish(h, recipe, None, source)
