# recipe-import

A FastAPI service that takes a recipe URL (or a photo/PDF of one) and creates the
recipe in [Mealie](https://mealie.io/), with an LLM fallback for anything the
structured scrapers can't parse.

Runs as a small self-hosted service (originally built to sit next to a homelab Mealie
instance) that a phone shortcut, a share-sheet action, or a webhook can call directly.

## Why this exists

Most recipe sites carry [recipe-scrapers](https://github.com/hhursev/recipe-scrapers)-
or JSON-LD-compatible markup, and Mealie can already import those on its own. This
service exists for everything that isn't: a YouTube recipe video, a screenshotted
Instagram post, a photographed cookbook page, a PDF flyer. It runs a source-specific
extractor first, and only calls an LLM when that comes back empty - so plain sites
never pay for a model call.

## How it decides

1. **Classify** the input: URL vs. uploaded file, and if a URL, which extractor
   applies (`src/classify.py`).
2. **Try structured extraction first** - Mealie's own `create/url` for scrapable
   sites, `recipe-scrapers`/`extruct`/`trafilatura` for the rest, `yt-dlp` +
   transcript for YouTube (`src/sources/`).
3. **Fall back to an LLM** only when step 2 returns nothing usable, with the recipe
   schema enforced via structured output (`src/llm.py`, `src/schema.py`) and a retry
   on transient overload before giving up.
4. **Guard against Mealie's own placeholder recipes** - `create/url` returns HTTP 201
   even for a page with no recipe on it, filled with sentinel text instead of an
   error. `mealie_client.is_placeholder()` catches this and falls through to step 2/3
   instead of treating it as a successful import.
5. **Write to Mealie, tag it, notify** - the created recipe gets an `auto-import` tag
   and a Home Assistant push notification with a link back to it
   (`src/mealie_client.py`, `src/ha_notify.py`).

See `DESIGN.md` for the full design record, including things that were tried and
rejected.

## Endpoints

| Path | Method | Purpose |
|---|---|---|
| `/healthz` | GET | liveness check |
| `/import` | POST | `{"url": "..."}` - queues an import from a URL |
| `/import/file` | POST | multipart upload - queues an import from a photo/PDF, bearer-token protected (`IMPORT_TOKEN`) |

Imports are queued and processed in the background; the caller gets a `202` and finds
out the result via the Home Assistant push notification.

## Running it

```bash
cp .env.example .env   # fill in real values
docker compose -f deploy/compose.yaml up -d --build
```

See `.env.example` for every configuration value and which are required.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

Tests run entirely offline against recorded fixtures (`tests/fixtures/`) - no network
access, no real Mealie/HA/LLM credentials needed (`tests/conftest.py` sets placeholder
env vars before any test module imports `config.py`).

## Stack

FastAPI, SQLite (import history / idempotency), `recipe-scrapers` + `extruct` +
`trafilatura` + `yt-dlp` for structured extraction, an LLM (OpenAI-compatible API) as
fallback and for title cleanup. Runs as a single container, arm64-compatible
(originally deployed on a Raspberry Pi).

## License

MIT, see `LICENSE`.
