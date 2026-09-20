"""`mealie_client.recipe_exists`: entscheidet, ob ein `done`-Eintrag im Store noch
durch ein Rezept in Mealie gedeckt ist (siehe `app._notify_if_done`).

Nur eine 404 darf als "weg" gelten. Jede andere Störung muss True liefern, sonst löst
ein kurz nicht erreichbares Mealie einen zweiten Import derselben Quelle aus - genau
die Dublette, die die Hash-Prüfung verhindern soll.
"""
from __future__ import annotations

import pytest
import requests

import mealie_client

# Beim Import von conftest.py noch die echte Funktion - die autouse-Fixture dort
# ersetzt sie erst beim Aufbau jedes Tests durch "Rezept ist noch da".
_REAL_RECIPE_EXISTS = mealie_client.recipe_exists


@pytest.fixture(autouse=True)
def real_recipe_exists(monkeypatch):
    """Dieses Modul prüft genau die Funktion, die `conftest.recipe_still_in_mealie`
    sonst überall wegmockt - hier gilt wieder das Original, nur `requests.get` ist
    ersetzt."""
    monkeypatch.setattr(mealie_client, "recipe_exists", _REAL_RECIPE_EXISTS)


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def test_missing_recipe_is_reported_as_gone(monkeypatch):
    monkeypatch.setattr(
        mealie_client.requests, "get", lambda url, headers=None, timeout=None: _FakeResponse(404, "not found")
    )

    assert mealie_client.recipe_exists("sweet-potato-taco-bowl") is False


def test_existing_recipe_is_reported_as_present(monkeypatch):
    monkeypatch.setattr(
        mealie_client.requests, "get", lambda url, headers=None, timeout=None: _FakeResponse(200, "{}")
    )

    assert mealie_client.recipe_exists("sweet-potato-taco-bowl") is True


def test_unreachable_mealie_counts_as_present(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        raise requests.exceptions.ConnectionError("kein Netz")

    monkeypatch.setattr(mealie_client.requests, "get", fake_get)

    assert mealie_client.recipe_exists("sweet-potato-taco-bowl") is True


def test_server_error_counts_as_present(monkeypatch):
    monkeypatch.setattr(
        mealie_client.requests, "get", lambda url, headers=None, timeout=None: _FakeResponse(500, "boom")
    )

    assert mealie_client.recipe_exists("sweet-potato-taco-bowl") is True
