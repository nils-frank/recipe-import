"""HTTP-Einstiegspunkt für recipe-import, siehe DESIGN.md §5 (Ablauf) und §6
(`src/app.py (A4)`, Uploadweg A12).

`POST /import` und `POST /import/file` antworten sofort `202` und verarbeiten im Hintergrund über
`BackgroundTasks`. Bei jedem Fehlschlag landet ein Grund in `store` und beim Nutzer per
`ha_notify`, nie ein stiller Abbruch (DESIGN.md §5, §7).

Seit A20 (openspec/changes/add-transient-retry-queue) gilt das "bewusst keine Queue"
aus DESIGN.md §5 nur noch für den Regelweg: ein Fehlschlag, bei dem die Gegenstelle
selbst "später nochmal" sagt (Sprachmodell ausgelastet, YouTube gedrosselt, Mealie
nicht erreichbar), wird nicht verworfen, sondern mit einer Fälligkeit geparkt und von
einer Hintergrundschleife erneut versucht - siehe `is_transient`, `_queue_transient`
und `_run_due_once`. Alles andere scheitert unverändert sofort.

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
import contextlib
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, Request, UploadFile

import config
import ha_notify
import image
import mealie_client
import model_chain
import naming
import payload
import schedule
import store
from classify import classify, normalize_url, url_hash
from llm import extract_recipe, extract_recipe_from_images
from schema import to_jsonld
from sources import document, site, youtube

# Präfix der `url`-Spalte für Uploads, damit Datei- und URL-Importe in derselben Tabelle
# unterscheidbar bleiben (DESIGN.md §5: "datei:<name>").
FILE_URL_PREFIX = "datei:"

# Zweites Tag neben "auto-import", wenn das Bild des Rezepts aus der Bildstufe stammt
# (A19). Ein erzeugtes Bild zeigt nicht das Gericht, das jemand gekocht hat; in Mealie
# soll das filterbar bleiben, statt nur im Protokoll zu stehen.
IMAGE_TAG = "ki-bild"

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


# Rückmeldung, wenn ein Datei-Import weiterlaufen soll, seine Bytes aber nicht mehr da
# sind - nach einem Neustart ohne aufbewahrte Uploads, oder wenn das Aufbewahrte fehlt.
FILE_LOST_MESSAGE = "Der Neustart kam mitten in der Verarbeitung. Bitte die Datei noch einmal teilen."


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
    resumed = store.pending()

    # Aufräumen vor der Wiederaufnahme, aber nach dem Einlesen von `pending()`: behalten
    # wird, was noch zu einer wartenden oder gerade laufenden Zeile gehört. Ein Absturz
    # zwischen Endzustand und Löschen hinterlässt sonst Bytes, die niemand mehr abholt
    # (A20, design.md).
    keep = {row["url_hash"] for row in resumed} | {row["url_hash"] for row in store.queued()}
    payload.sweep(keep)

    for entry in resumed:
        if entry["url"].startswith(FILE_URL_PREFIX):
            # Seit A20 kann ein Datei-Import den Neustart überstehen: liegen die Bytes
            # als aufbewahrter Upload vor, läuft er weiter. Ohne sie gilt weiterhin, was
            # DESIGN.md §5 beschreibt - gespeichert wäre dann nur der Hash, und statt
            # endlos "pending" zu bleiben, wird der Eintrag als gescheitert markiert und
            # der Mensch gebeten, die Datei erneut zu teilen.
            uploads = payload.load(entry["url_hash"], entry["payload"])
            if uploads is None:
                log.warning(
                    "Datei-Import %s nicht wiederaufnehmbar: %s", entry["url_hash"], FILE_LOST_MESSAGE
                )
                _fail_permanently(entry["url_hash"], FILE_LOST_MESSAGE)
                continue
            log.info("Nehme unterbrochenen Datei-Import wieder auf: %s", entry["url"])
            _track(asyncio.create_task(_run_file_import(entry["url_hash"], entry["url"], uploads)))
            continue
        log.info("Nehme unterbrochenen Import wieder auf: %s", entry["url"])
        # Direkt _run_import statt _process_import: die Zeile kommt aus store.pending()
        # und ist damit bereits exklusiv "pending" - dieser Container ist beim Start ihr
        # einziger Besitzer. _process_import würde über store.start() den atomaren Claim
        # aus Befund 3 (Review A8) erneut versuchen, der auf einem schon pending-
        # Eintrag scheitert (die WHERE-Klausel lässt nur `failed` -> `pending` zu) und
        # die Wiederaufnahme verhindern würde.
        _track(asyncio.create_task(_run_import(entry["url_hash"], entry["url"])))

    poller = asyncio.create_task(_poll_queue())
    sucher = asyncio.create_task(_refresh_models())
    try:
        yield
    finally:
        # Sauber abräumen, sonst meldet asyncio beim Herunterfahren eine nie
        # abgewartete Aufgabe - und ein halb gelaufener Versuch bliebe als `pending`
        # stehen, den der nächste Start ohnehin wieder aufnimmt.
        for aufgabe in (poller, sucher):
            aufgabe.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await aufgabe


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
    hat_bearer = bool(authorization) and authorization.lower().startswith("bearer ")
    supplied = authorization[7:].strip() if hat_bearer else ""
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

    # Name, Typ und Grösse jeder Datei noch vor der Hash-Prüfung ins Protokoll: wird ein
    # Upload gleich als Dublette abgewiesen, ist das sonst die einzige Stelle, an der
    # überhaupt steht, welche Datei der Kurzbefehl geschickt hat.
    log.info(
        "POST /import/file: %s",
        ", ".join(f"{u.filename} ({u.content_type or 'ohne Typ'}, {len(u.data)} Bytes)" for u in uploads),
    )

    _enforce_rate_limit(f"{FILE_URL_PREFIX}{uploads[0].filename}")

    background_tasks.add_task(_process_file, uploads)
    return {"status": "accepted"}


# --- Warteschlange für vorübergehende Fehlschläge (A20) --------------------------
#
# Dispatch über den Klassennamen wie in `_describe_source_error` und aus demselben
# Grund (siehe Moduldocstring). Wer hier steht, sagt selbst "später nochmal": der
# Anbieter des Sprachmodells wegen eigener Auslastung, YouTube wegen Drosselung,
# Mealie, weil es gar nicht erreichbar war. Alles andere ist ein endgültiger
# Fehlschlag und behält den Wortlaut aus DESIGN.md §7.
_TRANSIENT_MESSAGES = {
    "LlmOverloadedError": (
        "Das Sprachmodell ist gerade ausgelastet. Ich versuche es automatisch später noch einmal."
    ),
    "ThrottledError": (
        "YouTube drosselt gerade die Untertitel. Ich versuche es automatisch später noch einmal."
    ),
    "MealieUnavailableError": (
        "Mealie ist gerade nicht erreichbar. Ich versuche es automatisch später noch einmal."
    ),
}

QUEUED_TITLE = "Import später"
FAILED_TITLE = "Import fehlgeschlagen"

# Nach der letzten Wiederholung oder beim Erreichen der Altersgrenze. Genau eine
# Meldung, nicht eine je Versuch (Anforderung "A queued import is visible to the
# person").
GIVE_UP_MESSAGE = "Auch nach mehreren Versuchen hat es nicht geklappt. Bitte noch einmal teilen."

# Der Rückfall, wenn der Fehlschlag zwar vorübergehend war, das Aufbewahren der
# hochgeladenen Bytes aber am Platzbudget scheitert (QUEUE_PAYLOAD_MAX_MB). Dann gibt
# es nichts zu parken, und der Import endet hier.
PAYLOAD_BUDGET_MESSAGE = (
    "Für einen späteren Versuch ist kein Speicher mehr frei. Bitte die Datei später noch einmal teilen."
)


def is_transient(exc: Exception) -> bool:
    """True, wenn die Gegenstelle nur vorübergehend nicht konnte - dann wird der Import
    geparkt statt verworfen (A20, Anforderung "Transient upstream failures are queued
    instead of failed")."""
    return type(exc).__name__ in _TRANSIENT_MESSAGES


def _describe_queued(exc: Exception) -> str:
    """Der Wortlaut der einen Meldung, die beim Parken herausgeht: er nennt den Grund
    in derselben schlichten Sprache wie DESIGN.md §7 und sagt zu, dass der Import nicht
    verloren ist."""
    return _TRANSIENT_MESSAGES[type(exc).__name__]


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
        # versuchen. Seit A20 wird dieser Fall geparkt; dieser Wortlaut bleibt als
        # Rückfall für den Fall, dass das Parken selbst nicht möglich war.
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
        # erneuten Versuch. Seit A20 derselbe Rückfall wie dort.
        return "Das Sprachmodell ist gerade überlastet. Bitte später erneut teilen."
    if kind == "LlmError":
        return f"Die Rezepterkennung ist gescheitert: {exc}"
    return f"Import fehlgeschlagen: {exc}"


def _fail_permanently(h: str, message: str) -> None:
    """Endgültiger Fehlschlag: Eintrag auf `failed`, aufbewahrte Uploads weg, genau eine
    Meldung an den Menschen. Eine Stelle für alle Wege, damit das Löschen der Bytes
    nicht an einem der Zweige vergessen wird (A20)."""
    payload.delete(h)
    store.fail(h, message)
    ha_notify.notify(FAILED_TITLE, message)


def _queue_transient(h: str, exc: Exception, uploads: list[document.Upload] | None = None) -> None:
    """Parkt einen vorübergehend gescheiterten Import für einen späteren Versuch (A20).

    Drei Ausgänge, in dieser Reihenfolge:

    1. Die Grenzen sind erreicht (Zahl der Versuche oder Alter seit der Annahme) - der
       Import scheitert endgültig mit genau einer letzten Meldung.
    2. Es sind Uploads aufzubewahren, aber das Platzbudget ist voll - der Import
       scheitert endgültig, statt den Inhalt eines anderen wartenden Eintrags zu
       verdrängen.
    3. Sonst wird geparkt: Fälligkeit rechnen, Versuchszähler erhöhen, Bytes aufbewahren.

    Die Meldung "wird später erneut versucht" geht nur beim ersten Parken heraus, nicht
    bei jedem Versuch - so verlangt es die Anforderung "No notification per attempt"."""
    now = datetime.now(UTC)
    row = store.find(h) or {}
    attempts = (row.get("attempts") or 0) + 1
    created_at = row.get("created_at") or now.isoformat()

    if schedule.should_give_up(attempts, created_at, now):
        log.info("Import %s wird nach %d Versuch(en) aufgegeben", h, attempts)
        _fail_permanently(h, GIVE_UP_MESSAGE)
        return

    stored = None
    if uploads is not None:
        if not payload.fits_in_budget(uploads):
            log.warning(
                "Kein Platz mehr fuer aufbewahrte Uploads (%d Bytes belegt, Grenze %s MB), "
                "Import %s scheitert endgueltig",
                payload.total_bytes(), config.QUEUE_PAYLOAD_MAX_MB, h,
            )
            _fail_permanently(h, PAYLOAD_BUDGET_MESSAGE)
            return
        stored = payload.save(h, uploads)

    due = schedule.next_due(attempts, now)
    store.queue(h, due.isoformat(), attempts, stored)
    log.info("Import %s geparkt: Versuch %d faellig am %s (%s)", h, attempts, due.isoformat(), exc)

    if attempts == 1:
        ha_notify.notify(QUEUED_TITLE, _describe_queued(exc))


async def _run_due_once(now: datetime | None = None) -> int:
    """Ein Durchgang der Warteschlange: alle fälligen Einträge, nacheinander.

    Eigene Koroutine statt eines Schleifenrumpfs, damit Tests sie mit festem `now`
    direkt aufrufen können, ohne zu schlafen und ohne die Schleife zu starten
    (DESIGN.md §12). Gibt die Zahl der bearbeiteten Einträge zurück.

    Nacheinander und nicht nebenläufig: geparkt wurde, weil eine Gegenstelle gerade
    nicht mehr konnte - mehr gleichzeitige Anfragen wären die falsche Antwort darauf."""
    moment = now or datetime.now(UTC)
    rows = store.due(moment.isoformat())
    for row in rows:
        h = row["url_hash"]
        if schedule.should_give_up(row["attempts"] or 0, row["created_at"], moment):
            log.info("Faelliger Import %s hat seine Grenze erreicht, kein weiterer Versuch", h)
            _fail_permanently(h, GIVE_UP_MESSAGE)
            continue
        if not store.claim_due(h):
            # Zwischen `due()` und hier hat jemand anders die Zeile übernommen.
            continue
        await _retry_due(row)
    return len(rows)


async def _retry_due(row: dict) -> None:
    """Ein fälliger Eintrag, bereits als `pending` übernommen, läuft wieder an.

    Bewusst ohne `store.start()`: der atomare Claim dort lässt nur `failed -> pending`
    zu, und vor allem bliebe `created_at` sonst nicht stehen - daran hängt
    `store.recent_count()`, und ein Wiederholungsversuch ist kein neu angenommener
    Import (DESIGN.md §11). Ab hier ist ein Versuch von einem ersten Anlauf nicht mehr
    zu unterscheiden."""
    h, url = row["url_hash"], row["url"]
    if not url.startswith(FILE_URL_PREFIX):
        await _run_import(h, url)
        return

    uploads = payload.load(h, row["payload"])
    if uploads is None:
        log.warning("Aufbewahrte Dateien zu %s fehlen, kein weiterer Versuch", h)
        _fail_permanently(h, FILE_LOST_MESSAGE)
        return
    await _run_file_import(h, url, uploads)


async def _poll_queue() -> None:
    """Die eine Hintergrundschleife (A20, design.md "One scheduler task, polling a due
    time"). Erst arbeiten, dann schlafen: ein Eintrag, der während der Ausfallzeit
    fällig geworden ist, läuft damit gleich nach dem Start und nicht erst einen Takt
    später.

    Ein Takt kostet eine indizierte SQLite-Abfrage; die Taktrate ist grob gegenüber
    Verzögerungen, die in Minuten gemessen werden. Ein Fehlschlag im Rumpf beendet die
    Schleife nicht - sonst stünde die Warteschlange bis zum nächsten Neustart still."""
    while True:
        try:
            await _run_due_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Durchgang der Warteschlange fehlgeschlagen, weiter beim naechsten Takt")
        await asyncio.sleep(config.QUEUE_POLL_SECONDS)


async def _refresh_models() -> None:
    """Die Modellsuche (A21): einmal beim Start, danach alle LLM_MODEL_REFRESH_SECONDS.

    Der Start wartet **nicht** darauf. Die Kette ist allein aus der Konfiguration
    benutzbar, und die Suche kann sie nur verbessern - ein Anbieter, der die Modellliste
    hängen lässt, darf `/healthz` nicht verzögern. Deshalb eine eigene Aufgabe, und
    deshalb `to_thread`: `model_chain.refresh()` ist blockierender `requests`-Code und
    gehört nicht in die Ereignisschleife.

    Wie `_poll_queue`: erst arbeiten, dann schlafen, und ein Fehlschlag im Rumpf beendet
    die Schleife nicht.
    """
    while True:
        try:
            await asyncio.to_thread(model_chain.refresh)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Modellsuche fehlgeschlagen, weiter beim naechsten Durchlauf")
        await asyncio.sleep(config.LLM_MODEL_REFRESH_SECONDS)


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
        log.info(
            "Import %s läuft bereits oder wartet auf einen späteren Versuch, "
            "überspringe doppelten Anlauf", h,
        )
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
        log.warning(
            "Platzhalter-Prüfung für %s (%s) nicht möglich, werte als Erfolg: %s", slug, normalized, exc
        )
        return slug, None

    if not mealie_client.is_placeholder(recipe_data):
        return slug, recipe_data

    log.info(
        "import_url(%s) hat nur ein Platzhalter-Rezept angelegt (slug=%s), lösche und "
        "versuche die nächste Stufe",
        normalized,
        slug,
    )
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
                recipe = result.recipe
                if recipe is None:
                    recipe = extract_recipe(result.text, normalized)
        elif kind == "youtube":
            result = youtube.fetch(normalized)
            recipe = extract_recipe(result.text, normalized)
        else:
            # Wird bereits am HTTP-Eingang abgewiesen (400), siehe import_recipe().
            # Diese Verzweigung ist eine Absicherung für den Wiederaufnahme-Pfad beim
            # Start, der ohne erneute HTTP-Validierung läuft.
            raise ValueError(f"nicht unterstützte URL: {normalized}")
    except Exception as exc:  # noqa: BLE001 - jeder Fehlschlag wird gemeldet, nie still verworfen
        if is_transient(exc):
            # Die Gegenstelle sagt selbst "später nochmal" (A20): der Import wird
            # geparkt statt verworfen, alles andere behält Wortlaut und Verhalten aus
            # DESIGN.md §7.
            log.warning("Extraktion für %s vorübergehend gescheitert: %s", h, exc)
            _queue_transient(h, exc)
            return
        message = _describe_source_error(exc)
        log.error("Extraktion fehlgeschlagen für %s: %s", h, message)
        _fail_permanently(h, message)
        return

    _publish(h, recipe, slug, normalized, mealie_data)


def _notify_if_done(h: str) -> bool:
    """True, wenn zu diesem Hash bereits ein fertiges Rezept existiert - dann geht nur
    der alte Link heraus (DESIGN.md §5, Schritt 2). Gilt für URLs wie für Dateien, weil
    `content_hash` denselben Hash-Raum benutzt.

    Ein `done`-Eintrag allein reicht dafür nicht: wird das Rezept später in Mealie
    gelöscht, bleibt der Eintrag stehen und hat am 2026-09-20 zwei Importe derselben
    Datei stumm abgewiesen, mit einem Link auf ein Rezept, das es nicht mehr gab.
    Deshalb wird der Slug vor der Rückmeldung gegen Mealie geprüft; fehlt er dort, geht
    der Eintrag auf `failed` zurück und `store.start` darf ihn erneut übernehmen."""
    existing = store.find(h)
    if not existing or existing["status"] != "done":
        return False

    slug = existing.get("slug")
    if not slug or not mealie_client.recipe_exists(slug):
        log.info("Eintrag %s ist done, das Rezept fehlt aber in Mealie - neuer Anlauf", h)
        store.fail(h, "Rezept in Mealie nicht mehr vorhanden")
        return False

    link = mealie_client.recipe_link(slug)
    log.info("Quelle bereits importiert (%s), sende alten Link %s", h, link)
    # Der Link steht zusätzlich im Meldungstext, nicht nur in `data.url`: antippen
    # öffnet zwar das Rezept, aber eine weggewischte oder in der Mitteilungszentrale
    # gelesene Meldung lässt sonst keinen Weg dorthin.
    ha_notify.notify(
        "Rezept schon vorhanden",
        f"{existing['title']} wurde bereits importiert\n{link}",
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


def _attach_image(slug: str, title: str, recipe, mealie_data: dict | None) -> bool:
    """Bildstufe (A19): erzeugt ein Bild und hängt es an das Rezept, wenn es noch keines
    hat. Gibt zurück, ob ein Bild angelegt wurde - danach richtet sich das Tag.

    Läuft erst, wenn das Rezept in Mealie existiert und `store.finish()` gelaufen ist,
    und wirft nie: ein Rezept ohne Bild ist ein kosmetischer Mangel, kein gescheiterter
    Import (wie `set_tags` darunter).

    Woher "hat schon ein Bild" kommt, hängt am Weg:
    - `recipe` liegt vor (JSON-LD-Weg, also auch Upload und YouTube): `to_jsonld` kennt
      kein Bildfeld, das Rezept ist immer ohne Bild.
    - Mealie hat selbst geschabt: die Antwort liegt als `mealie_data` schon vor
      (geholt von `_reject_if_placeholder`), ein zweiter GET wäre dieselbe Anfrage ein
      zweites Mal. Trägt sie ein Bild, bleibt es stehen - ein von der Quellseite
      geholtes Foto zeigt das echte Gericht und wird nie ersetzt.
    - `mealie_data` fehlt (die Platzhalter-Prüfung hat Mealie nicht erreicht): kein
      Bildaufruf. Bei unbekanntem Stand ist Nichtstun richtig, dieselbe Stelle, an der
      auch die Namensstufe aufgibt.

    "Hat schon ein Bild" beantwortet `mealie_client.has_image()` über Mealies
    Mediendatei, nicht über das Feld `image` - warum, steht dort und im Moduldocstring
    von `mealie_client`."""
    if not config.IMAGE_ENABLED:
        # Vor der Bildstandsprüfung, nicht erst in image.generate(): seit diese Prüfung
        # Mealies Mediendatei abfragt (Aufgabe 1.2, 2026-09-20) ist sie selbst ein
        # Netzaufruf, und eine abgeschaltete Stufe soll gar keinen verursachen.
        log.info("Bildstufe ist per IMAGE_ENABLED abgeschaltet, kein Bild für %s", slug)
        return False

    try:
        if recipe is not None:
            ingredients = list(recipe.recipeIngredient)
        elif mealie_data is None:
            log.warning("Kein Rezeptinhalt aus Mealie für %s, überspringe die Bildstufe", slug)
            return False
        elif mealie_client.has_image(mealie_data):
            log.info("Rezept %s hat bereits ein Bild, keine Bilderzeugung", slug)
            return False
        else:
            # Die Zubereitung geht nicht mehr in den Prompt ein, siehe prompts.py.
            ingredients, _ = mealie_client.recipe_texts(mealie_data)

        data = image.generate(title, ingredients)
        if data is None:
            return False

        mealie_client.set_image(slug, data)
    except Exception as exc:  # noqa: BLE001 - Bildstufe darf den Import nie scheitern lassen
        log.warning("Bildstufe für %s fehlgeschlagen, Rezept bleibt ohne erzeugtes Bild: %s", slug, exc)
        return False

    log.info("Erzeugtes Bild an %s angehängt", slug)
    return True


def _publish(
    h,
    recipe,
    slug: str | None,
    source: str,
    mealie_data: dict | None = None,
    uploads: list[document.Upload] | None = None,
) -> None:
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
    versendete Link gültig bleibt.

    Feature A19 (Bildstufe): nach `store.finish()` und vor `set_tags` liegt
    `_attach_image` - erst ab da existiert das Rezept sicher, und ein Fehlschlag der
    Stufe kann keinen zweiten Import mehr auslösen. Ob ein Bild entstanden ist,
    entscheidet allein über das zweite Tag; an Rückmeldung und Link ändert sich
    nichts."""
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
        if is_transient(exc):
            # Mealie war gar nicht erreichbar, hat also nichts abgelehnt (A20,
            # `MealieUnavailableError`): parken statt scheitern lassen. Ein
            # Statusfehler von Mealie bleibt ein endgültiger Fehlschlag.
            log.warning("Mealie für %s vorübergehend nicht erreichbar: %s", h, exc)
            _queue_transient(h, exc, uploads)
            return
        message = f"Mealie hat den Import abgelehnt: {exc}"
        log.error("Mealie-Import fehlgeschlagen für %s: %s", h, message)
        _fail_permanently(h, message)
        return

    # Ab hier ist das Rezept in Mealie angelegt: sofort persistieren, damit ein
    # gleich folgender set_tags()-Fehlschlag keine Doppelanlage mehr auslösen kann.
    store.finish(h, slug, title)
    # Endzustand erreicht, aufbewahrte Uploads werden nicht mehr gebraucht (A20).
    payload.delete(h)

    # Bildstufe vor set_tags und vor der Meldung (A19): so bleibt es ein einziges PATCH
    # für beide Tags, und die Push-Meldung heisst weiterhin "das Rezept ist fertig" -
    # antippen zeigt es samt Bild. Das kostet bis zu einen Bild-Timeout Wartezeit, wie
    # die Namensstufe zuvor auch.
    tags = ["auto-import"]
    if _attach_image(slug, title, recipe, mealie_data):
        tags.append(IMAGE_TAG)

    try:
        mealie_client.set_tags(slug, tags)
    except Exception as exc:  # noqa: BLE001 - fehlendes Tag ist kosmetisch, kein Abbruchgrund
        # Auch ein nicht erreichbares Mealie wird hier **nicht** geparkt (A20,
        # design.md): das Rezept existiert bereits und der Eintrag ist `done`, ein
        # fehlendes Tag ist kosmetisch. Ein zweiter Anlauf würde ein zweites Rezept
        # anlegen.
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
        log.info(
            "Datei-Import %s läuft bereits oder wartet auf einen späteren Versuch, "
            "überspringe doppelten Anlauf", h,
        )
        return

    await _run_file_import(h, label, uploads)


async def _run_file_import(h: str, label: str, uploads: list[document.Upload]) -> None:
    """Der Datei-Import ab dem Punkt, an dem die Zeile diesem Aufruf gehört.

    Getrennt von `_process_file`, damit ein fälliger Versuch und die Wiederaufnahme
    beim Start denselben Weg nehmen wie der erste Anlauf, ohne `store.start()` erneut
    zu versuchen (A20) - genauso, wie `_run_import` vom URL-Weg getrennt ist."""
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
        if is_transient(exc):
            # Wie im URL-Weg, mit einem Unterschied: die hochgeladenen Bytes müssen
            # aufbewahrt werden, sonst hätte der spätere Versuch nichts zu lesen (A20).
            log.warning("Datei-Import %s vorübergehend gescheitert: %s", h, exc)
            _queue_transient(h, exc, uploads)
            return
        message = _describe_source_error(exc)
        log.error("Datei-Import fehlgeschlagen für %s: %s", h, message)
        _fail_permanently(h, message)
        return

    _publish(h, recipe, None, source, uploads=uploads)
