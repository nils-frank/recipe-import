"""URL-Normalisierung, Hashing und Quellenerkennung fuer den Rezeptimport.

Eine Rezept-URL kommt oft in mehreren gleichwertigen Schreibweisen herein -
mit Tracking-Parametern vom Teilen-Menü, als `youtu.be`-Kurzlink, als
`/shorts/`-Pfad oder ueber `m.youtube.com`. normalize_url() bringt sie auf
eine kanonische Form, damit store.find() per Hash echte Duplikate erkennt
statt eine zweite Kopie desselben Rezepts anzulegen (Ablauf-Schritt 2 in
DESIGN.md Abschnitt 5).
"""
from __future__ import annotations

import hashlib
import re
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

SourceKind = Literal["site", "youtube", "unsupported"]

_YOUTUBE_HOSTS = {"www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be"}

# utm_* ist ein Praefix (utm_source, utm_medium, ...), die uebrigen sind
# einzelne bekannte Tracking-Parameter, siehe DESIGN.md Abschnitt 6.
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_PARAMS = {"fbclid", "gclid", "si"}

_SHORTS_PATH_RE = re.compile(r"^/shorts/([^/?#]+)")


def _extract_youtube_id(parsed) -> str | None:
    host = parsed.netloc.lower()

    if host == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/")[0]
        return video_id or None

    shorts_match = _SHORTS_PATH_RE.match(parsed.path)
    if shorts_match:
        return shorts_match.group(1)

    if parsed.path == "/watch":
        params = dict(parse_qsl(parsed.query))
        return params.get("v")

    return None


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()

    if host in _YOUTUBE_HOSTS:
        video_id = _extract_youtube_id(parsed)
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
        # Unbekanntes YouTube-URL-Muster (z.B. ein Kanal- oder Playlist-Link):
        # normal weiterverarbeiten statt eine Video-ID zu erfinden. classify()
        # erkennt die Domain trotzdem als "youtube".

    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    kept = sorted(
        (k, v) for k, v in pairs
        if k not in _TRACKING_PARAMS and not k.startswith(_TRACKING_PREFIXES)
    )
    query = urlencode(kept)

    path = parsed.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return urlunparse((parsed.scheme, host, path, parsed.params, query, ""))


def url_hash(url: str) -> str:
    normalized = normalize_url(url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def classify(url: str) -> SourceKind:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        return "unsupported"
    host = parsed.netloc.lower()
    if host in _YOUTUBE_HOSTS:
        return "youtube"
    return "site"
