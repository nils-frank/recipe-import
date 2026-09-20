# Design

## Context

See proposal.md for motivation. What shapes the approach:

- The pipeline already has one optional, never-fatal LLM stage: `naming.make_name()`
  (A18). It reuses `llm._post` as transport, is switched by a single config flag,
  returns `None` on every failure, and is disabled by an autouse fixture in the test
  suite. The picture stage is the same shape, one layer further out.
- All four import paths converge in `app._publish()`. That is the single place where the
  recipe is known to exist in Mealie and its slug is final, including the slug change
  Mealie performs on rename.
- Of those paths, only `mealie_client.import_url()` (Mealie's own scraper) can produce a
  recipe that already has an image. On the `create_from_jsonld` path, `schema.to_jsonld`
  carries no `image` field at all, so the recipe is always pictureless.
- `tests/conftest.py` requires the suite to run without any network access, so the stage
  must be reachable through a single monkeypatchable seam and off by default in tests.
- The project's convention for anything touching Mealie's API is a live check against the
  running instance, recorded in `mealie_client.py` with the date and what the probe
  returned. That convention applies to both new Mealie calls in this change.

## Goals / Non-Goals

**Goals:**

- One new stage, wired in at exactly one place, reusing the existing HTTP transport and
  error conventions.
- A failure mode that is structurally incapable of losing an import: the stage runs only
  after `store.finish()`, so nothing it does can cause a second import of the same
  source.
- No new mandatory configuration and no new dependency.

**Non-Goals:**

- No regeneration, no "try again with a different prompt", no second image call.
- No image editing, resizing, cropping or re-encoding in this service. Mealie already
  derives its own thumbnails from the uploaded original.
- No backfill of recipes imported before this change. The stage runs during an import
  only.
- No change to the notification wording, the endpoints, or the rate limit.

## Decisions

### Where the stage runs: inside `_publish`, after `store.finish()`, before tags and notification

`_publish()` is the one place all four paths pass through with a final slug. Placing the
stage after `store.finish()` means an exception or a hang there can never make a
completed import look unfinished, which is the same reasoning that moved `store.finish()`
ahead of `set_tags()` (Review A8, finding 1).

Running it before the push notification costs the user up to one image-generation
timeout of extra waiting for the push. Accepted, because the naming stage already delays
the push by up to 60s for the same reason: the notification means "finished", and
tapping it should show the finished recipe, picture included. It also keeps the tag write
to a single `set_tags()` call that either carries the extra tag or does not.

Alternative considered: notify first, attach the picture afterwards. Rejected. It makes
the push arrive sooner but splits the tag write into two PATCH calls and lets the user
open a recipe that visibly changes under them a few seconds later.

### Deciding whether an image is needed: from the data already in hand, never a new GET

`_publish()` already receives `mealie_data` for the scraped path (fetched once by
`_reject_if_placeholder`, deliberately reused rather than re-fetched). The rule:

- `slug is None` on entry (the `create_from_jsonld` path, including uploads and YouTube):
  the recipe cannot have an image, generate.
- scraped path with `mealie_data` present: generate only if its image field is empty.
- scraped path with `mealie_data is None` (Mealie was unreachable during the placeholder
  check): skip the stage and log it. Unknown state is not worth an image call, and this
  is the same branch on which the naming stage already gives up.

Alternative considered: always `get_recipe(slug)` right before the stage. Rejected as a
second request for information the function already holds, which is the exact duplication
that `_reject_if_placeholder` was written to avoid.

What Mealie puts in the recipe's image field when there is no picture (`null`, an empty
string, or a sentinel such as `"no image"`) must be established by a live probe before
this branch is written, and recorded in `mealie_client.py` with the date, like
`is_placeholder`. The check belongs in `mealie_client` as `has_image(recipe: dict) ->
bool`, next to `is_placeholder`, because it is knowledge about Mealie's response shape.

**Probe result, 2026-09-20 (task 1.2), which overturns the paragraph above:** the field
carries no such value. Mealie fills `image` with a short random key (`'QziM'`) whether or
not a picture exists - 28 of this installation's 29 pictureless recipes have one, only a
single recipe has `null`. The key is a cache buster assigned at creation, not a statement
about a stored image. So the decision cannot be made from the data already in hand after
all: `has_image` asks for the media file itself (`GET
/api/media/recipes/{id}/images/original.webp`; 200 = picture, 404 = none, everything else
and any network error = "has a picture", to never overwrite a photo). That is one extra
GET per scraped import, accepted against a check that is simply wrong. The stage returns
before that request when `IMAGE_ENABLED` is false, so a disabled stage still makes no
call at all.

### Image provider: Pollinations.ai, called over plain HTTP GET

**Probe 2026-09-20 (task 1.3), first attempt:** the configured Gemini key cannot generate
images at all. Its OpenAI-compatible layer maps `/images/generations` onto `predict`,
which none of its 58 models support (404), and the image models over `generateContent`
answer 429 with `free_tier ... limit: 0` - for `gemini-2.5-flash-image`,
`gemini-3-pro-image(-preview)`, `gemini-3.1-flash-image(-preview)`,
`gemini-3.1-flash-lite-image` and `nano-banana-pro-preview`. A key from a freshly created
project in the same account answers the same way, so the image entitlement is zero at
account level. The text model answers 200 with the same key.

The stage therefore uses a second provider: **Pollinations.ai**, whose image endpoint is
free and needs no account.

**Probe 2026-09-20 (task 1.3), Pollinations:**

- Transport is a single `GET https://image.pollinations.ai/prompt/{urlencoded prompt}`.
  There is no JSON envelope in either direction: the response body *is* the image,
  `content-type: image/jpeg`.
- Unauthenticated works (`x-auth-status: unauthenticated`), no key, no account, no card.
- One image takes 35-46 s (four measurements) and arrives as a 768x768 JPEG of 46-73 KB.
- `width` and `height` are ignored without a token; every answer is 768x768.
- `nologo=true` is ignored without a token: every image carries a `pollinations.ai`
  watermark in the bottom right corner.
- **Second probe the same day, with a token from auth.pollinations.ai** (sent both as
  `Authorization: Bearer` and as `token=`, identical results): the token is recognised -
  `width=1024&height=768` now changes the answer (886x665, the aspect ratio honoured,
  the exact size not). `nologo=true` still is not: the watermark stays. So a token buys
  the size, not the watermark; removing the watermark needs a paid tier of that service.
  The watermark is therefore a property of this stage, not a setting.
- `GET /models` lists exactly one model for an anonymous caller: `sana`. Passing
  `model=flux` or `model=turbo` is accepted but served by the same model.
- Two requests in flight at once answer 429 with a 694 byte body; sequential requests are
  fine. One import makes at most one image call, so this is only reached if two imports
  overlap, and the stage then simply leaves that recipe without a picture.
- Prompt length matters for quality. The prompt written for task 4.1 (761 characters,
  mostly a list of things to leave out) produced an empty plate with scribbled
  pseudo-text on it. A single compact sentence (about 240 characters: dish name, main
  ingredients, plating, lighting) produced a usable photo of the dish. So this change
  replaces the long prompt with the compact one instead of keeping both - the long one has
  never run in production and no provider is asking for it.

Consequences carried into the design:

- The response handling written for `/images/generations` (`data[0].b64_json`, fallback
  `data[0].url`) does not apply to this provider at all. `image.py` gets a second, much
  shorter transport path, chosen by `IMAGE_PROVIDER`.
- The OpenAI-compatible path stays in the code. It is the escape hatch for the day a paid
  key exists, and it is what `IMAGE_BASE_URL`/`IMAGE_API_KEY` were introduced for.
- `IMAGE_API_KEY` must **not** keep falling back to `LLM_API_KEY` when the image provider
  is a different company. The fallback is correct only for the OpenAI-compatible path,
  where "same provider as the text model" is the default assumption.
- The watermark is a deviation from the spec sentence "no logos, no watermarks". Accepted
  rather than worked around: cropping or re-encoding needs an image library this service
  does not carry, and a visible mark on a generated picture is closer to the intent of the
  `ki-bild` tag than a clean one. Removing it is a matter of setting a Pollinations token
  in `IMAGE_API_KEY`, not of code.

Alternative considered: keep waiting for a Gemini image quota. Rejected, it is not a
quota that filling in time or creating projects moves, and the stage is otherwise
finished.

Alternative considered: another paid provider (OpenAI `gpt-image-1`, Stability) through
the existing OpenAI-compatible path. Not rejected - it needs no code beyond this change,
only `IMAGE_PROVIDER=openai` plus the two values. It costs money per import for a picture
on a tile, which is what pushed the free provider in front.

### Transport for the OpenAI-compatible path: `POST {base}/images/generations`, module `src/image.py`

Same OpenAI-compatible provider, different endpoint, so `llm._post` (which hard-codes
`/chat/completions` and a `response_format` JSON schema) does not fit. `src/image.py`
gets its own small `requests.post`, deliberately without the retry ladder from
`llm._post`: a transient provider error costs a picture here, not an import, and the
spec fixes one call per import.

Request: `{"model": IMAGE_MODEL, "prompt": ..., "n": 1, "response_format": "b64_json"}`.
Response handling accepts `data[0].b64_json`, and falls back to fetching `data[0].url`
when a provider answers with a URL instead. Anything else counts as an unusable answer.

The decoded bytes are validated with `llm._image_mime()`, which already maps JPEG, PNG
and WebP magic bytes and is the same check the upload path needs for Mealie's file
extension. Reusing it keeps one definition of "these are the image types we handle";
`naming.py` reaching into `llm._post` sets the precedent for this kind of reuse.

### Configuration: on by default, no new mandatory value

```
IMAGE_ENABLED    default true          same truthiness parsing as NAMING_ENABLED
IMAGE_PROVIDER   default pollinations  or openai for an OpenAI-compatible provider
IMAGE_MODEL      default sana          pinned id, never a floating "latest" alias
IMAGE_BASE_URL   default per provider  https://image.pollinations.ai, else LLM_BASE_URL
IMAGE_API_KEY    default per provider  empty for pollinations, else LLM_API_KEY
```

`config.require()` is not used for any of them, so the service cannot gain a new hard
startup failure from this change.

The base URL and the key default per provider, not globally. For `openai` they fall back
to the text model's values, because "the same provider does both" is the assumption that
path was written under. For `pollinations` the key defaults to empty: sending the text
model's key to a different company would leak it, and the provider needs no key. A
Pollinations token, which lifts the fixed 768x768 size but not the watermark, is set
explicitly in `IMAGE_API_KEY` and then travels as `Authorization: Bearer`.

Switching providers is one value plus, for a paid provider, two more. The model id stays
pinned either way.

### The generation prompt lives in `prompts.py` and is written in English

Every other prompt in the project is German because its output is German text. This
prompt's output is a picture with no text in it, so nothing German is lost, and image
models respond more predictably to English captions. The recipe content interpolated into
it (German dish name, German ingredients) is passed through unchanged.

The prompt names the dish, its main ingredients, the plating and the light, in one
compact sentence, and feeds a bounded excerpt of the recipe: the name and the first N
ingredients, shortened the same way `naming._shorten` already does. Reuse
`naming._shorten` rather than writing a second one.

The 2026-09-20 probe forced this shape. The first version spelled out everything the
picture must not contain, over 761 characters, and the model answered with exactly the
thing it was told to avoid: an empty plate carrying scribbled pseudo-text. The compact
sentence, with a short "no text, no logo" at the end, produced a usable photo of the
dish. Preparation steps are dropped from the prompt entirely - they lengthen it without
changing what the plate looks like.

### Marking: a second tag, set in the existing `set_tags` call

`set_tags(slug, [...])` replaces the tag list in one PATCH and creates missing tags, so
the mark costs nothing extra: the list becomes `["auto-import", "ki-bild"]` when a
picture was attached. The tag name follows the German wording used in user-facing strings
throughout the project.

Alternative considered: a note in the recipe description. Rejected, it edits content the
extraction stage produced and is not filterable in Mealie.

### Upload: `PUT /api/recipes/{slug}/image`, multipart, verified live first

Mealie's documented shape for attaching image bytes to an existing recipe is a multipart
`PUT` carrying the file and its extension. Path, field names, expected status and what
the response contains must be confirmed against the running instance via
`/openapi.json` before the call is written, and recorded in the `mealie_client.py` module
docstring with the date, exactly as the existing calls are.

This is the one call in the change that cannot be reasoned out from the code in this
repository, so it is the first implementation task, before anything depends on its shape.

Note that this call sends multipart, not JSON, so it cannot use the module-level
`_HEADERS` constant (which sets `Content-Type: application/json`); it needs the
`Authorization` header only and lets `requests` set the boundary.

### Testing: offline, with the stage off by default in the suite

`tests/conftest.py` gains an autouse fixture that sets `config.IMAGE_ENABLED = False`,
mirroring `naming_off`, so every existing test keeps asserting behavior independent of
this stage. `tests/test_image.py` switches it on explicitly and covers, with the HTTP
calls monkeypatched: a generated picture reaching the upload call and producing the extra
tag; a recipe that already has an image making no call at all; a provider error, an
unusable answer and a rejected upload each leaving the import `done`, notified, and
untagged.

## Risks / Trade-offs

- [The push notification arrives up to one image timeout later than today] → Timeout kept
  well under the extraction timeouts (target 90s), the stage is switchable, and the
  notification still means "the recipe is complete".
- [Cost per import rises sharply: an image call is far more expensive than the text calls
  this service makes, and the stage is on by default] → It runs only for recipes that
  have no picture, never on the duplicate path, at most once per import, and the existing
  `RATE_LIMIT_PER_HOUR` caps the blast radius. `IMAGE_ENABLED=false` is a one-line
  rollback in `.env`.
- [A generated picture can look convincing and is not the dish anyone cooked] → Every such
  recipe carries the `ki-bild` tag, and a scraped real photo is never replaced.
- [The provider's image endpoint or model id may differ from what this design assumes] →
  Both are verified live in the first implementation tasks before code depends on them,
  and the base URL and model are configuration, not constants.
- [Every generated picture carries a visible `pollinations.ai` watermark on the free
  tier] → Accepted, see the provider decision. A token in `IMAGE_API_KEY` removes it
  without a code change.
- [A free service with no contract can slow down, start charging, or drop its anonymous
  tier at any time] → The stage never fails an import, so the worst case is recipes
  without pictures and a warning in the log. `IMAGE_PROVIDER` plus two values move the
  stage to a paid provider.
- [Recipe names and ingredients travel to a third party in a URL, which is logged on the
  way] → Same class of data the extraction stage already sends to the text model, and no
  personal data: a dish name and a shopping list. Worth knowing before pointing this at
  anything else.
- [Mealie's image field semantics are assumed, not measured] → Live probe task before the
  branch is written; if the probe cannot be run, the safe reading is "has an image" and
  the stage simply does less.
- [An upload that succeeds after Mealie already rendered the recipe could leave a stale
  thumbnail in a client's cache] → Not handled; a refresh in Mealie resolves it and no
  data is at risk.

## Migration Plan

No data migration. The store schema, the endpoints and the notification payloads are
unchanged.

- Deploy: rebuild the container, no new `.env` values required - the default provider
  needs no key. Optionally set `IMAGE_PROVIDER`/`IMAGE_MODEL` or the provider overrides
  first.
- Verify on one real import of a source that has no picture (a photo upload is the
  cheapest), checking that the recipe in Mealie shows a picture and carries both tags.
- Rollback: set `IMAGE_ENABLED=false` and restart. Pictures already attached stay; they
  can be identified in Mealie by the `ki-bild` tag.
