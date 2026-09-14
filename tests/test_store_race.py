"""Test zu Review-A8-Befund 3 (recipe-import-plan.md, Auftragsdetails A13): der
Wettlauf zwischen `find` und `start` wird atomar in SQLite geschlossen, nicht durch
Lesen-dann-Schreiben in Python.

Ohne die Reparatur war `store.start()` ein bedingungsloses `INSERT ... ON CONFLICT DO
UPDATE`: zwei fast gleichzeitige Aufrufe für denselben Hash haben beide den Eintrag auf
"pending" gesetzt und liefen beide durch die ganze Kette - eine doppelte Mealie-Anlage.
Jetzt lässt die WHERE-Klausel der ON-CONFLICT-Aktualisierung nur `failed -> pending`
zu; `start()` gibt zurück, ob der Aufruf den Eintrag exklusiv gewonnen hat.
"""
from __future__ import annotations

from classify import url_hash


def test_second_start_on_same_pending_hash_does_not_claim(isolated_store):
    url = "https://example.com/rezepte/wettlauf"
    h = url_hash(url)

    first = isolated_store.start(h, url)
    second = isolated_store.start(h, url)

    assert first is True
    assert second is False
    assert isolated_store.find(h)["status"] == "pending"


def test_start_reclaims_a_failed_entry(isolated_store):
    """Bestehendes Verhalten (DESIGN.md §6): ein erneuter Versuch derselben URL nach
    einem Fehlschlag muss weiterhin möglich sein."""
    url = "https://example.com/rezepte/erneuter-versuch"
    h = url_hash(url)

    isolated_store.start(h, url)
    isolated_store.fail(h, "vorheriger Fehlschlag")

    reclaimed = isolated_store.start(h, url)

    assert reclaimed is True
    entry = isolated_store.find(h)
    assert entry["status"] == "pending"
    assert entry["error"] is None


def test_start_does_not_reclaim_a_done_entry(isolated_store):
    """Absicherung auf Store-Ebene: DESIGN.md §5 Schritt 2 prüft `find()` vor jedem
    `start()`-Aufruf und startet auf einem `done`-Eintrag gar nicht erst - `start()`
    selbst darf einen solchen Eintrag trotzdem nicht versehentlich zurücksetzen."""
    url = "https://example.com/rezepte/schon-fertig"
    h = url_hash(url)

    isolated_store.start(h, url)
    isolated_store.finish(h, "schon-fertig", "Schon Fertig")

    claimed = isolated_store.start(h, url)

    assert claimed is False
    assert isolated_store.find(h)["status"] == "done"
