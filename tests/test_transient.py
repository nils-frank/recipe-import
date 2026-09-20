"""Einteilung in "vorübergehend" und "endgültig" (Aufgaben 4.1 bis 4.3, A20).

Die Einteilung ist die Weiche der ganzen Änderung: was hier als vorübergehend gilt,
wird geparkt und automatisch wiederholt, alles andere behält den Wortlaut und das
Verhalten aus DESIGN.md §7.
"""
from __future__ import annotations

import requests

import app as app_module
import mealie_client
from llm import LlmError, LlmOverloadedError, NoRecipeFoundError
from sources.document import UnreadablePdfError, UnsupportedFileError
from sources.youtube import NoTranscriptError, SourceError, ThrottledError


class _Response:
    def __init__(self, status_code: int, text: str = "kaputt"):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def test_connection_failure_and_status_error_are_different_classes(monkeypatch):
    """Der Kern von Aufgabe 4.1: "Mealie war nicht da" und "Mealie hat nein gesagt"
    müssen sich unterscheiden lassen, ohne Fehlertexte zu lesen."""
    monkeypatch.setattr(
        mealie_client.requests,
        "post",
        lambda *a, **kw: (_ for _ in ()).throw(requests.ConnectionError("no route to host")),
    )
    try:
        mealie_client.create_from_jsonld({})
    except mealie_client.MealieError as exc:
        unreachable = exc
    assert isinstance(unreachable, mealie_client.MealieUnavailableError)

    monkeypatch.setattr(mealie_client.requests, "post", lambda *a, **kw: _Response(500))
    try:
        mealie_client.create_from_jsonld({})
    except mealie_client.MealieError as exc:
        rejected = exc
    assert type(rejected) is mealie_client.MealieError


def test_timeout_is_also_unavailable(monkeypatch):
    monkeypatch.setattr(
        mealie_client.requests,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(requests.Timeout("zu langsam")),
    )

    try:
        mealie_client.get_recipe("irgendein-slug")
    except mealie_client.MealieError as exc:
        caught = exc

    assert isinstance(caught, mealie_client.MealieUnavailableError)


def test_unavailable_is_still_caught_as_mealie_error():
    """Unterklasse, damit jedes bestehende `except MealieError` unverändert greift."""
    assert issubclass(mealie_client.MealieUnavailableError, mealie_client.MealieError)


def test_is_transient_is_true_for_the_three_wait_cases():
    assert app_module.is_transient(LlmOverloadedError("503")) is True
    assert app_module.is_transient(ThrottledError("429")) is True
    assert app_module.is_transient(mealie_client.MealieUnavailableError("kein Netz")) is True


def test_is_transient_is_false_for_every_permanent_case():
    permanent = [
        NoRecipeFoundError("Auf dem Bild war kein Rezept zu erkennen."),
        NoTranscriptError("keine Untertitel"),
        SourceError("kein Rezept"),
        UnreadablePdfError("gescannt"),
        UnsupportedFileError("HEIC"),
        LlmError("Schema zweimal verfehlt"),
        mealie_client.MealieError("422: abgelehnt"),
        ValueError("etwas ganz anderes"),
    ]

    assert [app_module.is_transient(exc) for exc in permanent] == [False] * len(permanent)


def test_each_transient_class_has_its_own_wording():
    messages = {
        type(exc).__name__: app_module._describe_queued(exc)
        for exc in (
            LlmOverloadedError("503"),
            ThrottledError("429"),
            mealie_client.MealieUnavailableError("kein Netz"),
        )
    }

    assert messages["LlmOverloadedError"].startswith("Das Sprachmodell ist gerade ausgelastet")
    assert messages["ThrottledError"].startswith("YouTube drosselt gerade die Untertitel")
    assert messages["MealieUnavailableError"].startswith("Mealie ist gerade nicht erreichbar")
    # Jede Meldung sagt zu, dass der Import nicht verloren ist - das ist der Zweck der
    # Meldung, nicht der Grund.
    assert all("automatisch später noch einmal" in text for text in messages.values())
    assert len(set(messages.values())) == 3
