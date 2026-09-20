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
5. **Write to Mealie** - via JSON-LD import, or by renaming what Mealie scraped itself
   (`src/mealie_client.py`).
6. **Generate a picture, but only if there is none** - three of the four paths create a
   recipe with no image at all. Those get one generated from the recipe's own content
   and uploaded (`src/image.py`). A photo Mealie scraped from the source page is never
   replaced, and a recipe that got a generated picture carries a `ki-bild` tag so it
   stays clear which pictures show the actual dish. Pictures come from Pollinations.ai
   by default (free, no key, and each one carries that provider's watermark);
   `IMAGE_PROVIDER=openai` points the stage at an OpenAI-compatible provider instead.
   Switchable via `IMAGE_ENABLED`.
7. **Tag it and notify** - the recipe gets an `auto-import` tag and a Home Assistant
   push notification with a link back to it (`src/ha_notify.py`).
8. **Retry instead of giving up when an upstream says "try later"** - a model at
   capacity, YouTube throttling subtitles, or an unreachable Mealie parks the import
   with a due time rather than discarding it (`src/schedule.py`, `src/payload.py`,
   `src/store.py`). Anything else - no recipe on the page, an unreadable PDF, a recipe
   Mealie rejected with an HTTP status - still fails right away with the same wording
   as before.

See `DESIGN.md` for the full design record, including things that were tried and
rejected.

## Endpoints

| Path | Method | Purpose |
|---|---|---|
| `/healthz` | GET | liveness check |
| `/import` | POST | `{"url": "..."}` - accepts an import from a URL |
| `/import/file` | POST | multipart upload - accepts an import from a photo/PDF, bearer-token protected (`IMPORT_TOKEN`) |

Imports are accepted and processed in the background; the caller gets a `202` and finds
out the result via the Home Assistant push notification.

## When an upstream is busy

An import that fails because something upstream was only temporarily unavailable is not
thrown away. It is parked with a due time and retried automatically by a single
background loop in the same process - no broker, no second container, and the schedule
lives in SQLite, so it survives a restart.

What the person sees:

- one notification when the import is parked, naming the reason ("Das Sprachmodell ist
  gerade ausgelastet. Ich versuche es automatisch später noch einmal.") - never a silent
  park, and never one notification per attempt;
- the normal "Rezept angelegt" notification with the link once a retry succeeds;
- one final "Import fehlgeschlagen" asking for the source again, if the retries run out
  (`QUEUE_MAX_ATTEMPTS`) or the import gets too old (`QUEUE_MAX_AGE_HOURS`).

Timing is exponential backoff in minutes with jitter; once the delay would exceed
`QUEUE_OFFPEAK_THRESHOLD_MINUTES`, the import is placed at a random point in the next
off-peak window (`QUEUE_OFFPEAK_WINDOW`, local time) instead, where capacity is easier
to get. An uploaded photo or PDF keeps its bytes next to the database while it waits, so
the retry never asks for the file again; they are deleted as soon as the import is done,
failed, or abandoned. Retries do not consume the hourly rate limit a second time - it
counts accepted imports, not attempts. See `.env.example` for every `QUEUE_*` value.

## When the model runs out of quota

The text stage does not use one pinned model. `LLM_MODEL_CHAIN` holds an ordered list,
newest first - by default `gemini-3.8-flash, gemini-3.7-flash, gemini-3.6-flash,
gemini-3.5-flash`. The first entry is the model normally used; the rest only ever answer
when an earlier one cannot.

This is not hypothetical. Measured against the deployed key on 2026-09-20:
`gemini-3.6-flash`, the model this service used to pin, answered
`429 "You exceeded your current quota"` on every request, while `gemini-3.8-flash` and
`gemini-3.5-flash` answered `200` in the same minute.

What moves a call to the next model:

- **two `429`** in a row - the model's quota is read as gone, and it is skipped for
  `LLM_MODEL_COOLDOWN_SECONDS` (default one hour) so a used-up quota costs one rejected
  call rather than one per import;
- **two `502`/`503`/`504`** - that is one model under load, not the provider; no cooldown
  follows, so it is back in first place on the very next call;
- **a `404`** or a `400` naming an unknown model - the name is dropped for the lifetime
  of the process, so a chain entry the provider retired never fails an import.

`500` keeps the old behaviour and does not switch models. Only when the whole chain is
used up does the import get parked and retried, exactly as described above - the change
adds no new failure text. The memory of exhausted models lives in the running process; a
restart starts again at the head of the chain.

### Newer models are adopted automatically

With `LLM_MODEL_AUTODISCOVER=true` (the default) the service reads the provider's model
list at startup and then once a day, keeps the names matching `LLM_MODEL_PATTERN`, and
puts a model newer than the current head in front of the chain. To see the same list
yourself - it is the exact request the service makes:

```bash
curl -s -H "Authorization: Bearer $LLM_API_KEY" "$LLM_BASE_URL/models" \
  | jq -r '.data[].id'
```

Ids come back with a `models/` prefix (`models/gemini-3.8-flash`); the service strips it.

A newly discovered model is never used for a real import until it has answered one
minimal request with `response_format: json_schema` and `strict` - the contract the whole
text stage depends on. A model that fails that probe is logged and discarded. When the
head does change, one push notification names the old and the new model, so an
unexplained change in recipe or title quality has an obvious suspect.

To go back to a single pinned model, set `LLM_MODEL_AUTODISCOVER=false` and
`LLM_MODEL_CHAIN` to that one name.

## Running it

```bash
cp .env.example .env   # fill in real values
docker compose -f deploy/compose.yaml up -d --build
```

See `.env.example` for every configuration value and which are required.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check src tests
pytest
```

The linter config lives in `pyproject.toml`; CI runs exactly the same two commands, so a
green local run is a green build.

Tests run entirely offline against recorded fixtures (`tests/fixtures/`) - no network
access, no real Mealie/HA/LLM credentials needed (`tests/conftest.py` sets placeholder
env vars before any test module imports `config.py`).

## Stack

FastAPI, SQLite (import history, idempotency, and the retry queue), `recipe-scrapers` +
`extruct` + `trafilatura` + `yt-dlp` for structured extraction, an LLM (OpenAI-compatible API) as
fallback, for title cleanup and for generating a recipe picture when the source has
none. Runs as a single container, arm64-compatible (originally deployed on a Raspberry
Pi).

## License

MIT, see `LICENSE`.
