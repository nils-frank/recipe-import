"""Test 7 aus DESIGN.md §12: ha_notify.notify() wirft auch dann nicht, wenn Home
Assistant mit 500 antwortet - und ebenso nicht bei einem Verbindungsfehler."""
from __future__ import annotations

import requests

import ha_notify


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def test_notify_does_not_raise_on_ha_500(monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        return _FakeResponse(500, "internal server error")

    monkeypatch.setattr(ha_notify.requests, "post", fake_post)

    ha_notify.notify("Rezept angelegt", "Testrezept", "http://mealie.local/g/home/r/testrezept")

    # Der einzige Kanal muss trotz Fehlschlag versucht worden sein, siehe DESIGN.md §6.
    assert len(calls) == 1


def test_notify_does_not_raise_on_connection_error(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        raise requests.exceptions.ConnectionError("kein Netz")

    monkeypatch.setattr(ha_notify.requests, "post", fake_post)

    ha_notify.notify("Import fehlgeschlagen", "Die Rezepterkennung ist gescheitert: Testfehler")
