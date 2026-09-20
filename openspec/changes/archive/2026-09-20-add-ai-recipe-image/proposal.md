# Proposal

## Why

Three of the four import paths produce a recipe with no picture at all: the JSON-LD path
(`create/html-or-json` fed from `to_jsonld`, which carries no `image` field), the
photo/PDF upload path, and YouTube. In Mealie's grid view such a recipe is a grey tile,
which is exactly the same complaint that motivated the naming stage (A18): name and
picture are the only two things visible before opening a recipe. Only Mealie's own
scraper path (`create/url`) usually brings a real photo along.

The service already talks to an OpenAI-compatible provider for extraction and naming, so
generating a picture for the recipes that have none is a third stage in the same shape as
the other two, and adds no dependency.

The picture itself comes from a second provider. The configured Gemini key can generate
text but not images: every image model answers `free_tier ... limit: 0`, on a brand new
project too, so the entitlement is zero for the whole account (probe 2026-09-20, task
1.3). The stage therefore calls **Pollinations.ai**, which generates images over a plain
HTTP GET without a key or an account, and keeps the OpenAI-compatible path as the
configurable alternative for a paid key.

## What Changes

- New optional pipeline stage after the recipe exists in Mealie: if the recipe carries no
  image, generate one from the recipe's own name, ingredients and steps and upload it to
  Mealie.
- New module `src/image.py` (generation) plus a Mealie upload call in
  `src/mealie_client.py`, and an image prompt in `src/prompts.py`.
- Stage runs only when Mealie reports no image for the recipe. A picture that Mealie
  scraped from the source page is never replaced: a real photo of the dish beats a
  generated one.
- Recipes that received a generated picture get a second tag next to `auto-import`, so it
  stays visible and filterable in Mealie which pictures show the actual dish and which
  were invented.
- New configuration: `IMAGE_ENABLED` (default on), `IMAGE_PROVIDER` (`pollinations` by
  default, `openai` for an OpenAI-compatible provider), `IMAGE_MODEL`, and optional
  `IMAGE_BASE_URL` / `IMAGE_API_KEY` overrides. The two overrides default per provider:
  for `openai` to the existing `LLM_*` values, for `pollinations` to that provider's URL
  and no key, so the text model's key is never sent to another company.
- Two transports in `src/image.py`: a GET whose answer is the image bytes
  (Pollinations), and the OpenAI-compatible `POST /images/generations` with a JSON
  envelope. Which one runs is `IMAGE_PROVIDER`, nothing else changes between them.
- Every generated picture carries the Pollinations watermark. A free token in
  `IMAGE_API_KEY` lifts the fixed 768x768 size but not the watermark; that needs a paid
  tier. The watermark is accepted, see design.md.
- The stage never fails an import. Any error (provider down, unusable answer, Mealie
  rejects the upload) is logged and the import completes without a picture, exactly like
  the naming stage.
- No change to the endpoints, to the `202` contract, or to the notification wording.

## Capabilities

### New Capabilities
- `recipe-image`: generating a picture for an imported recipe that has none, attaching it
  in Mealie, and marking it as generated.

### Modified Capabilities
<!-- None: the project has no specs under openspec/specs/ yet, so there is no existing
     capability whose requirements change. The pipeline behavior touched by this change
     (tagging, notification) is described inside the new capability. -->

## Impact

- Code: `src/app.py` (one call in `_publish`, plus the tag list), `src/mealie_client.py`
  (image upload, image-present check), `src/config.py`, `src/prompts.py`, new
  `src/image.py`, new `tests/test_image.py`.
- Docs: `README.md` (pipeline description, stack), `.env.example` (new variables),
  `DESIGN.md` (new module section, configuration, feedback texts).
- Dependencies: none added. The generation call is `requests` (a GET for Pollinations, a
  POST for the OpenAI-compatible path); the Mealie upload is a multipart `requests` call.
- New outbound target: `image.pollinations.ai`. The recipe name and its main ingredients
  travel there inside the URL.
- Timing: a Pollinations image took 35-46 s in the probe, and the stage runs before the
  push notification, so an import without a picture today gets that much slower.
- Cost and rate limit: the default provider is free and unmetered, but answers 429 while
  another of its requests is still running. The existing `RATE_LIMIT_PER_HOUR` (20) stays
  the only ceiling, now with one image call per import in the worst case. On a paid
  provider that image call is markedly more expensive than every text call of an import
  together.
- Mealie: needs write access to the recipe image endpoint with the existing
  `MEALIE_TOKEN`. The endpoint path and its expected form must be verified live against
  the running instance before implementation, following the project's convention of
  recording such checks with a date in `mealie_client.py`.
