"""Die Warteschlange im Zusammenspiel (Aufgaben 5.2, 5.3, 6.1 bis 6.4, 7.1 bis 7.4,
8.2 und 8.3 der Änderung A20).

Alle Gegenstellen sind ersetzt, und die Zeit kommt als Argument: `_run_due_once(now)`
wird direkt aufgerufen, die Schleife läuft nie, und kein Test schläft (DESIGN.md §12).
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

import app as app_module
import config
import mealie_client
import payload
from classify import url_hash
from conftest import FIXTURES_DIR
from llm import LlmOverloadedError
from schema import Recipe
from sources import SourceError, SourceResult

URL = "https://example.com/rezepte/pfannkuchen"
JPEG_BYTES = (FIXTURES_DIR / "document_rezeptfoto.jpg").read_bytes()

RECIPE = Recipe(
    name="Pfannkuchen",
    recipeIngredient=["Mehl", "Milch", "Ei"],
    recipeInstructions=["Verrühren", "Backen"],
)


@pytest.fixture
def notes(monkeypatch):
    """Alle Push-Meldungen dieses Tests, in der Reihenfolge ihres Versands."""
    sent: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        app_module.ha_notify, "notify", lambda title, message, link=None: sent.append((title, message, link))
    )
    return sent


@pytest.fixture
def payload_dir(tmp_path, monkeypatch):
    directory = tmp_path / "queue"
    monkeypatch.setattr(config, "QUEUE_PAYLOAD_DIR", str(directory))
    return directory


@pytest.fixture(autouse=True)
def no_upstream(monkeypatch):
    """Nichts davon darf erreicht werden, ohne dass ein Test es ausdrücklich setzt."""
    def forbidden(*args, **kwargs):
        raise AssertionError("unerwarteter Aufruf einer Gegenstelle")

    monkeypatch.setattr(app_module.mealie_client, "import_url", forbidden)
    monkeypatch.setattr(app_module.mealie_client, "create_from_jsonld", forbidden)
    monkeypatch.setattr(app_module.mealie_client, "set_tags", lambda slug, tags: None)
    monkeypatch.setattr(app_module.mealie_client, "recipe_link", lambda slug: f"https://mealie/{slug}")
    monkeypatch.setattr(app_module.site, "fetch", forbidden)
    monkeypatch.setattr(app_module, "extract_recipe", forbidden)
    monkeypatch.setattr(app_module, "extract_recipe_from_images", forbidden)


def _site_yields(monkeypatch, outcome):
    """Mealie kann die Seite nicht selbst schaben, der Text geht ans Sprachmodell -
    `outcome` ist entweder ein `Recipe` oder eine zu werfende Ausnahme."""
    monkeypatch.setattr(app_module.mealie_client, "import_url", lambda url: None)
    monkeypatch.setattr(
        app_module.site,
        "fetch",
        lambda url: SourceResult(recipe=None, text="Pfannkuchen: Mehl, Milch, Ei", title=None, stage="text"),
    )

    def extract(text, source):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(app_module, "extract_recipe", extract)


def _mealie_creates(monkeypatch, slug="pfannkuchen"):
    created: list[dict] = []

    def create(data):
        created.append(data)
        return slug

    monkeypatch.setattr(app_module.mealie_client, "create_from_jsonld", create)
    return created


# --- 6.1 URL-Weg ----------------------------------------------------------------


def test_overloaded_model_parks_a_url_import(monkeypatch, isolated_store, notes, payload_dir):
    _site_yields(monkeypatch, LlmOverloadedError("503"))

    asyncio.run(app_module._process_import(URL))

    row = isolated_store.find(url_hash(URL))
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert row["due_at"] > datetime.now(UTC).isoformat()
    assert row["payload"] is None
    assert notes == [("Import später", app_module._TRANSIENT_MESSAGES["LlmOverloadedError"], None)]


def test_permanent_failure_still_fails_immediately(monkeypatch, isolated_store, notes, payload_dir):
    _site_yields(monkeypatch, SourceError("kein Rezept"))

    asyncio.run(app_module._process_import(URL))

    row = isolated_store.find(url_hash(URL))
    assert row["status"] == "failed"
    assert row["due_at"] is None
    assert notes == [("Import fehlgeschlagen", "Auf der Seite war kein Rezept zu finden.", None)]


# --- 6.2 Datei-Weg --------------------------------------------------------------


def _photo_import_overloaded(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "extract_recipe_from_images",
        lambda images, source: (_ for _ in ()).throw(LlmOverloadedError("503")),
    )
    return [app_module.document.Upload(filename="foto.jpg", content_type="image/jpeg", data=JPEG_BYTES)]


def test_photo_import_keeps_its_bytes_when_parked(monkeypatch, isolated_store, notes, payload_dir):
    uploads = _photo_import_overloaded(monkeypatch)

    asyncio.run(app_module._process_file(uploads))

    h = app_module.document.content_hash(uploads)
    row = isolated_store.find(h)
    assert row["status"] == "queued"
    assert json.loads(row["payload"])["files"][0]["filename"] == "foto.jpg"
    restored = payload.load(h, row["payload"])
    assert [u.data for u in restored] == [JPEG_BYTES]
    assert notes[0][0] == "Import später"


# --- 5.2 Platzbudget ------------------------------------------------------------


def test_full_budget_fails_the_import_and_spares_the_others(
    monkeypatch, isolated_store, notes, payload_dir
):
    fremd = [app_module.document.Upload(filename="alt.jpg", content_type="image/jpeg", data=b"x" * 900)]
    payload.save("fremder-eintrag", fremd)
    monkeypatch.setattr(config, "QUEUE_PAYLOAD_MAX_MB", 1000 / (1024 * 1024))
    uploads = _photo_import_overloaded(monkeypatch)

    asyncio.run(app_module._process_file(uploads))

    h = app_module.document.content_hash(uploads)
    assert isolated_store.find(h)["status"] == "failed"
    assert notes == [("Import fehlgeschlagen", app_module.PAYLOAD_BUDGET_MESSAGE, None)]
    # Der Inhalt eines anderen wartenden Eintrags bleibt unangetastet.
    fremd = {"files": [{"stored": "00.bin", "filename": "alt.jpg", "content_type": "image/jpeg"}]}
    assert payload.load("fremder-eintrag", json.dumps(fremd))
    assert not (payload_dir / h).exists()


# --- 6.3 Mealie -----------------------------------------------------------------


def test_unreachable_mealie_parks_the_import(monkeypatch, isolated_store, notes, payload_dir):
    isolated_store.start(url_hash(URL), URL)
    monkeypatch.setattr(
        app_module.mealie_client,
        "create_from_jsonld",
        lambda data: (_ for _ in ()).throw(mealie_client.MealieUnavailableError("kein Netz")),
    )

    app_module._publish(url_hash(URL), RECIPE, None, URL)

    row = isolated_store.find(url_hash(URL))
    assert row["status"] == "queued"
    assert notes == [("Import später", app_module._TRANSIENT_MESSAGES["MealieUnavailableError"], None)]


def test_rejecting_mealie_still_fails_the_import(monkeypatch, isolated_store, notes, payload_dir):
    isolated_store.start(url_hash(URL), URL)
    monkeypatch.setattr(
        app_module.mealie_client,
        "create_from_jsonld",
        lambda data: (_ for _ in ()).throw(mealie_client.MealieError("422: abgelehnt")),
    )

    app_module._publish(url_hash(URL), RECIPE, None, URL)

    assert isolated_store.find(url_hash(URL))["status"] == "failed"
    assert notes[0][0] == "Import fehlgeschlagen"
    assert "Mealie hat den Import abgelehnt" in notes[0][1]


def test_unreachable_mealie_in_set_tags_stays_cosmetic(monkeypatch, isolated_store, notes, payload_dir):
    """Das Rezept existiert bereits - ein zweiter Anlauf würde ein zweites anlegen."""
    isolated_store.start(url_hash(URL), URL)
    _mealie_creates(monkeypatch)
    monkeypatch.setattr(
        app_module.mealie_client,
        "set_tags",
        lambda slug, tags: (_ for _ in ()).throw(mealie_client.MealieUnavailableError("kein Netz")),
    )

    app_module._publish(url_hash(URL), RECIPE, None, URL)

    assert isolated_store.find(url_hash(URL))["status"] == "done"
    assert notes == [("Rezept angelegt", "Pfannkuchen", "https://mealie/pfannkuchen")]


# --- 6.4 Dublette während des Wartens -------------------------------------------


def test_sharing_a_queued_url_again_starts_nothing(monkeypatch, isolated_store, notes, payload_dir):
    _site_yields(monkeypatch, LlmOverloadedError("503"))
    asyncio.run(app_module._process_import(URL))
    notes.clear()

    # Ab hier darf nichts mehr laufen: jede Gegenstelle ist wieder gesperrt.
    monkeypatch.setattr(app_module.mealie_client, "import_url", lambda url: 1 / 0)

    asyncio.run(app_module._process_import(URL))

    row = isolated_store.find(url_hash(URL))
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert notes == []


# --- 7.1 / 7.2 Ein Durchgang der Warteschlange ----------------------------------


def _queue_url_import(store, *, due: datetime, attempts: int = 1, created: datetime | None = None) -> str:
    h = url_hash(URL)
    store.start(h, URL)
    store.queue(h, due.isoformat(), attempts)
    if created is not None:
        with store._connect() as conn:
            conn.execute("UPDATE imports SET created_at = ? WHERE url_hash = ?", (created.isoformat(), h))
            conn.commit()
    return h


def test_a_due_entry_runs_and_succeeds(monkeypatch, isolated_store, notes, payload_dir):
    now = datetime.now(UTC)
    h = _queue_url_import(isolated_store, due=now - timedelta(minutes=1))
    created_before = isolated_store.find(h)["created_at"]
    _site_yields(monkeypatch, RECIPE)
    created = _mealie_creates(monkeypatch)

    handled = asyncio.run(app_module._run_due_once(now))

    assert handled == 1
    row = isolated_store.find(h)
    assert row["status"] == "done"
    assert row["due_at"] is None
    # Die Ratenbegrenzung zählt `created_at`, ein Versuch ist kein neuer Import.
    assert row["created_at"] == created_before
    assert len(created) == 1
    assert notes == [("Rezept angelegt", "Pfannkuchen", "https://mealie/pfannkuchen")]


def test_an_entry_that_is_not_due_stays_untouched(monkeypatch, isolated_store, notes, payload_dir):
    now = datetime.now(UTC)
    h = _queue_url_import(isolated_store, due=now + timedelta(minutes=30))

    assert asyncio.run(app_module._run_due_once(now)) == 0
    assert isolated_store.find(h)["status"] == "queued"
    assert notes == []


def test_a_failing_retry_is_parked_again_without_a_second_notification(
    monkeypatch, isolated_store, notes, payload_dir
):
    now = datetime.now(UTC)
    h = _queue_url_import(isolated_store, due=now - timedelta(minutes=1))
    _site_yields(monkeypatch, LlmOverloadedError("503"))

    asyncio.run(app_module._run_due_once(now))

    row = isolated_store.find(h)
    assert row["status"] == "queued"
    assert row["attempts"] == 2
    assert notes == []


def test_the_attempt_limit_ends_the_queue(monkeypatch, isolated_store, notes, payload_dir):
    monkeypatch.setattr(config, "QUEUE_MAX_ATTEMPTS", 3)
    now = datetime.now(UTC)
    h = _queue_url_import(isolated_store, due=now - timedelta(minutes=1), attempts=3)

    asyncio.run(app_module._run_due_once(now))

    row = isolated_store.find(h)
    assert row["status"] == "failed"
    assert row["error"] == app_module.GIVE_UP_MESSAGE
    assert notes == [("Import fehlgeschlagen", app_module.GIVE_UP_MESSAGE, None)]


def test_the_age_limit_ends_the_queue_without_calling_upstream(
    monkeypatch, isolated_store, notes, payload_dir
):
    monkeypatch.setattr(config, "QUEUE_MAX_AGE_HOURS", 24)
    now = datetime.now(UTC)
    h = _queue_url_import(
        isolated_store, due=now - timedelta(minutes=1), attempts=2, created=now - timedelta(hours=25)
    )

    # Jede Gegenstelle ist über `no_upstream` gesperrt: ein Aufruf wäre ein Fehler.
    asyncio.run(app_module._run_due_once(now))

    assert isolated_store.find(h)["status"] == "failed"
    assert notes == [("Import fehlgeschlagen", app_module.GIVE_UP_MESSAGE, None)]


def test_giving_up_deletes_the_retained_files(monkeypatch, isolated_store, notes, payload_dir):
    monkeypatch.setattr(config, "QUEUE_MAX_ATTEMPTS", 2)
    uploads = [app_module.document.Upload(filename="foto.jpg", content_type="image/jpeg", data=JPEG_BYTES)]
    h = app_module.document.content_hash(uploads)
    isolated_store.start(h, f"{app_module.FILE_URL_PREFIX}foto.jpg")
    meta = payload.save(h, uploads)
    now = datetime.now(UTC)
    isolated_store.queue(h, (now - timedelta(minutes=1)).isoformat(), 2, meta)

    asyncio.run(app_module._run_due_once(now))

    assert isolated_store.find(h)["status"] == "failed"
    assert not (payload_dir / h).exists()


def test_a_due_file_import_reads_its_retained_bytes(monkeypatch, isolated_store, notes, payload_dir):
    uploads = [app_module.document.Upload(filename="foto.jpg", content_type="image/jpeg", data=JPEG_BYTES)]
    h = app_module.document.content_hash(uploads)
    isolated_store.start(h, f"{app_module.FILE_URL_PREFIX}foto.jpg")
    meta = payload.save(h, uploads)
    now = datetime.now(UTC)
    isolated_store.queue(h, (now - timedelta(minutes=1)).isoformat(), 1, meta)

    seen: list[bytes] = []

    def extract(images, source):
        seen.extend(images)
        return RECIPE

    monkeypatch.setattr(app_module, "extract_recipe_from_images", extract)
    _mealie_creates(monkeypatch)

    asyncio.run(app_module._run_due_once(now))

    assert seen == [JPEG_BYTES]
    assert isolated_store.find(h)["status"] == "done"
    # Endzustand erreicht: die aufbewahrten Bytes sind weg.
    assert not (payload_dir / h).exists()
    assert notes == [("Rezept angelegt", "Pfannkuchen", "https://mealie/pfannkuchen")]


def test_a_due_file_import_without_its_bytes_asks_for_the_file(
    monkeypatch, isolated_store, notes, payload_dir
):
    h = "verlorene-datei"
    isolated_store.start(h, f"{app_module.FILE_URL_PREFIX}foto.jpg")
    now = datetime.now(UTC)
    isolated_store.queue(h, (now - timedelta(minutes=1)).isoformat(), 1, None)

    asyncio.run(app_module._run_due_once(now))

    assert isolated_store.find(h)["status"] == "failed"
    assert notes == [("Import fehlgeschlagen", app_module.FILE_LOST_MESSAGE, None)]


# --- 8.2 Von der ersten Drosselung bis zum Rezept --------------------------------


def test_two_transient_failures_then_success(monkeypatch, isolated_store, notes, payload_dir):
    """Ein Import scheitert zweimal vorübergehend und gelingt beim dritten Anlauf:
    genau ein Rezept in Mealie, genau eine Meldung beim Parken, genau eine zum
    Erfolg."""
    _site_yields(monkeypatch, LlmOverloadedError("503"))

    asyncio.run(app_module._process_import(URL))
    h = url_hash(URL)

    # Zweiter Anlauf, wieder ausgelastet: die Fälligkeit wird vorgezogen, damit sie
    # ohne Warten erreicht ist.
    now = datetime.now(UTC)
    isolated_store.queue(h, (now - timedelta(minutes=1)).isoformat(), 1)
    asyncio.run(app_module._run_due_once(now))
    assert isolated_store.find(h)["attempts"] == 2

    # Dritter Anlauf, jetzt antwortet das Modell.
    _site_yields(monkeypatch, RECIPE)
    created = _mealie_creates(monkeypatch)
    isolated_store.queue(h, (now - timedelta(minutes=1)).isoformat(), 2)
    asyncio.run(app_module._run_due_once(now))

    assert isolated_store.find(h)["status"] == "done"
    assert len(created) == 1
    assert [note[0] for note in notes] == ["Import später", "Rezept angelegt"]


# --- 5.3 / 7.3 / 7.4 / 8.3 Start und Herunterfahren ------------------------------


async def _through_lifespan(after=None) -> None:
    async with app_module.lifespan(app_module.app):
        # asyncio.create_task() plant nur - die Aufgaben brauchen einen Umlauf der
        # Event-Loop, um tatsächlich zu starten.
        await asyncio.sleep(0.05)
        if after is not None:
            after()


def test_startup_starts_the_poller_and_shutdown_cancels_it(monkeypatch, isolated_store, payload_dir):
    """7.3: die Schleife läuft ab dem Start und ist nach dem Herunterfahren beendet -
    keine Aufgabe, die asyncio als nie abgewartet meldet."""
    runs: list[datetime | None] = []

    async def fake_run(now=None):
        runs.append(now)
        return 0

    monkeypatch.setattr(app_module, "_run_due_once", fake_run)
    # Lange Taktrate: geprüft wird der erste Durchgang und das Abräumen, nicht das
    # Schlafen.
    monkeypatch.setattr(config, "QUEUE_POLL_SECONDS", 3600)

    running: list[set[asyncio.Task]] = []

    async def run() -> set[asyncio.Task]:
        async with app_module.lifespan(app_module.app):
            await asyncio.sleep(0.05)
            running.append({t for t in asyncio.all_tasks() if t is not asyncio.current_task()})
        return {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}

    remaining = asyncio.run(run())

    # Zwei Hintergrundaufgaben seit A21: diese Schleife und die Modellsuche.
    namen = {task.get_coro().__name__ for task in running[0]}
    assert namen == {"_poll_queue", "_refresh_models"}
    # Erst arbeiten, dann schlafen: ein während der Ausfallzeit fällig gewordener
    # Eintrag läuft gleich nach dem Start.
    assert runs == [None]
    assert all(task.done() for task in running[0])
    assert remaining == set()


def test_startup_retries_an_entry_that_became_due_while_down(monkeypatch, isolated_store, payload_dir, notes):
    """8.3: die Zeile übersteht den Neustart und läuft danach, ohne sich zu verdoppeln."""
    monkeypatch.setattr(config, "QUEUE_POLL_SECONDS", 3600)
    now = datetime.now(UTC)
    h = _queue_url_import(isolated_store, due=now - timedelta(minutes=5))
    _site_yields(monkeypatch, RECIPE)
    created = _mealie_creates(monkeypatch)

    asyncio.run(_through_lifespan())

    row = isolated_store.find(h)
    assert row["status"] == "done"
    assert len(created) == 1
    assert notes == [("Rezept angelegt", "Pfannkuchen", "https://mealie/pfannkuchen")]


def test_startup_leaves_a_future_due_time_alone(monkeypatch, isolated_store, payload_dir, notes):
    monkeypatch.setattr(config, "QUEUE_POLL_SECONDS", 3600)
    now = datetime.now(UTC)
    h = _queue_url_import(isolated_store, due=now + timedelta(hours=2))

    asyncio.run(_through_lifespan())

    assert isolated_store.find(h)["status"] == "queued"
    assert notes == []


def test_startup_resumes_a_file_import_with_retained_bytes(
    monkeypatch, isolated_store, payload_dir, notes
):
    monkeypatch.setattr(config, "QUEUE_POLL_SECONDS", 3600)
    uploads = [app_module.document.Upload(filename="foto.jpg", content_type="image/jpeg", data=JPEG_BYTES)]
    h = app_module.document.content_hash(uploads)
    label = f"{app_module.FILE_URL_PREFIX}foto.jpg"
    isolated_store.start(h, label)
    meta = payload.save(h, uploads)
    # Der Dienst wurde mitten in der Verarbeitung beendet: `pending`, aber mit
    # aufbewahrten Bytes aus einem vorherigen Parken.
    with isolated_store._connect() as conn:
        conn.execute("UPDATE imports SET payload = ? WHERE url_hash = ?", (meta, h))
        conn.commit()

    monkeypatch.setattr(app_module, "extract_recipe_from_images", lambda images, source: RECIPE)
    created = _mealie_creates(monkeypatch)

    asyncio.run(_through_lifespan())

    assert isolated_store.find(h)["status"] == "done"
    assert len(created) == 1
    assert notes == [("Rezept angelegt", "Pfannkuchen", "https://mealie/pfannkuchen")]


def test_startup_asks_again_for_a_file_import_without_retained_bytes(
    monkeypatch, isolated_store, payload_dir, notes
):
    monkeypatch.setattr(config, "QUEUE_POLL_SECONDS", 3600)
    h = "datei-ohne-bytes"
    isolated_store.start(h, f"{app_module.FILE_URL_PREFIX}foto.jpg")

    asyncio.run(_through_lifespan())

    assert isolated_store.find(h)["status"] == "failed"
    assert notes == [("Import fehlgeschlagen", app_module.FILE_LOST_MESSAGE, None)]


def test_startup_sweeps_orphan_payload_directories(monkeypatch, isolated_store, payload_dir, notes):
    monkeypatch.setattr(config, "QUEUE_POLL_SECONDS", 3600)
    uploads = [app_module.document.Upload(filename="foto.jpg", content_type="image/jpeg", data=b"bild")]
    wartend = "wartender-eintrag"
    isolated_store.start(wartend, f"{app_module.FILE_URL_PREFIX}foto.jpg")
    meta = payload.save(wartend, uploads)
    isolated_store.queue(wartend, (datetime.now(UTC) + timedelta(hours=2)).isoformat(), 1, meta)
    payload.save("verwaist", uploads)

    asyncio.run(_through_lifespan())

    assert (payload_dir / wartend).is_dir()
    assert not (payload_dir / "verwaist").exists()
