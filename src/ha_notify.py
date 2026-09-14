"""Rückmeldung an Home Assistant für recipe-import: Push-Notification an genau einen
`notify.*`-Kanal (`HA_NOTIFY_TARGET`), mit klickbarem Link zum importierten Rezept.

Ein reiner `notify.send_message`-Kanal ohne `data`-Feld eignet sich nicht: er kann
keinen klickbaren Mealie-Link tragen, nur einen Titel.

`notify()` wirft niemals - eine fehlgeschlagene Rückmeldung wird protokolliert, darf
aber einen bereits erfolgreichen Import nicht nachträglich zu einem Fehler machen.
"""
from __future__ import annotations

import logging

import requests

import config

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15


def notify(title: str, message: str, link: str | None = None) -> None:
    headers = {
        "Authorization": f"Bearer {config.HA_TOKEN}",
        "Content-Type": "application/json",
    }

    # data.url öffnet den Link beim Antippen der Push-Notification in der iOS
    # Companion App. Ohne Link (z.B. bei einer Fehlermeldung) bleibt data leer.
    mobile_payload: dict = {"title": title, "message": message}
    if link:
        mobile_payload["data"] = {"url": link}
    _post(
        f"{config.HA_URL}/api/services/notify/{config.HA_NOTIFY_TARGET}",
        headers,
        mobile_payload,
    )


def _post(url: str, headers: dict, payload: dict) -> None:
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 400:
            log.warning("HA-Rückmeldung fehlgeschlagen: %s -> %d: %s", url, resp.status_code, resp.text[:300])
    except requests.RequestException as exc:
        log.warning("HA-Rückmeldung fehlgeschlagen: %s -> %s", url, exc)
