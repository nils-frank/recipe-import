# Proposal

## Why

The picture stage talks to exactly one image provider: `config.IMAGE_PROVIDER`, default
`pollinations` (`src/image.py`). That default was not a preference, it was a measurement.
The probe recorded in `src/config.py` on 2026-09-20 found the Gemini account could not
generate images at all: every image model answered `429 "generate_content_free_tier_...
limit: 0"`, on a freshly created project of the same account too. Pollinations was the
only provider that returned a picture without a bill.

Two things follow from that decision, and both are visible in Mealie. Every generated
picture carries a `pollinations.ai` watermark in the bottom right corner, which a free
token does not remove, and every picture is 768x768 from the single model the free tier
serves (`sana`). The account now holds $5 of Gemini credit, so the measurement the default
rests on is out of date: the image models that answered "limit: 0" are the ones this
change wants to use first, with Pollinations kept underneath as the free floor for when
the credit is gone.

## What Changes

- **A chain of image candidates instead of one provider.** `IMAGE_MODEL_CHAIN` holds an
  ordered list of `provider:model` entries, best first. The first entry is what the
  service normally uses; the rest are fallbacks reached only when an earlier entry cannot
  deliver a picture. Proposed default:
  `gemini:gemini-3-pro-image, gemini:gemini-3.1-flash-image, pollinations:sana` - the
  paid Nano Banana Pro generation first, its cheaper flash sibling second, the free
  provider last. The exact Gemini ids are the ones task 1 proves against the credited
  key; no unverified name survives into the default.
- **A third provider form, `gemini`.** Gemini image models are not reachable over the
  OpenAI-compatible surface this service already uses for text: `/images/generations` maps
  to `predict` there, which none of its models serve (`404`, measured 2026-09-20). They
  answer on the native surface, `POST {base}/v1beta/models/{model}:generateContent`, with
  the picture returned as base64 `inlineData`. That is a different request and a different
  answer shape, so it becomes a third fetcher next to `pollinations` and `openai`.
- **A rejected candidate hands the call down the chain.** A candidate that answers "out of
  quota", "billing required" or a rate-limit status is marked exhausted and skipped for a
  configurable cooldown (`IMAGE_MODEL_COOLDOWN_SECONDS`), and the same prompt goes to the
  next candidate. A candidate the provider does not know (`404`, model-not-found) leaves
  the chain for the process lifetime. A candidate that answers with something unusable is
  skipped for the rest of that import.
- **The credit running out is an ordinary event, not a failure.** When the $5 is spent,
  the Gemini entries answer "exhausted", the chain slides to Pollinations, and imports
  keep getting pictures with a watermark instead of no picture at all. Nothing needs to be
  reconfigured for that transition, and nothing needs to be reconfigured to undo it when
  credit is topped up beyond the cooldown.
- **A bounded stage instead of a single bounded call.** Today's guarantee is "at most one
  image call per import". It becomes "at most one call per candidate, at most one usable
  picture, and never longer than `IMAGE_DEADLINE_SECONDS` in total". The deadline is what
  keeps a three-entry chain from making a push notification wait for three timeouts.
- **No new mandatory configuration.** An existing `.env` keeps working: an explicitly set
  `IMAGE_PROVIDER`/`IMAGE_MODEL` pair moves to the head of the chain and the rest become
  fallbacks below it, the same rule `LLM_MODEL` follows for the text chain (A21).
- Automatic discovery of newer image models, the text stage's equivalent (A21
  `LLM_MODEL_AUTODISCOVER`), is **out of scope**. Discovery there is cheap because the
  probe is one text call; an image probe costs an image. The image chain stays pinned and
  a newer model is a deliberate edit.
- No HTTP contract change, no new dependency, no new container, no change to what the user
  sees: the push notification, the `ki-bild` tag and the "never fails an import" rule are
  untouched.

## Capabilities

### New Capabilities

<!-- None. The picture stage is already specified as the `recipe-image` capability, and
     this change alters how that same capability chooses a provider. A second capability
     would split one stage across two specs. -->

### Modified Capabilities

- `recipe-image`: "The picture stage is configurable" changes from a single provider,
  model, base URL and key to an ordered chain of `provider:model` candidates with
  per-provider defaults, and gains the `gemini` provider form. "At most one image call per
  import" changes from one call per import to one call per candidate under a total
  deadline. New requirements cover the order of the chain, what moves a call to the next
  candidate, the cooldown memory for an exhausted candidate, and the fact that a picture
  from a fallback candidate is treated exactly like one from the head.

## Impact

- `src/image.py`: `generate` gains the candidate loop and the failure classification; the
  two existing fetchers stay, a `_fetch_gemini` joins them. The prompt, the size ceiling,
  the MIME check and the "never raises" contract are unchanged.
- New module for the chain state (order, cooldowns, per-import skip), so `image.py` keeps
  holding only transport and decoding. Same split as `llm.py` / `model_chain.py` (A21).
- `src/config.py`: `IMAGE_MODEL_CHAIN`, `IMAGE_MODEL_COOLDOWN_SECONDS`,
  `IMAGE_DEADLINE_SECONDS`, and per-provider resolution of base URL and key for the
  `gemini` form (native surface derived from `LLM_BASE_URL`, key from `LLM_API_KEY` -
  same provider, same account, so no key crosses a company boundary).
  `IMAGE_PROVIDER`, `IMAGE_MODEL`, `IMAGE_BASE_URL`, `IMAGE_API_KEY` and `IMAGE_ENABLED`
  keep working with their current meaning.
- `src/app.py`: no change. `_attach_image` calls `image.generate` and reads bytes or
  `None`, which is exactly what it will keep getting.
- `tests/`: new tests for chain parsing and defaults, the Gemini request shape and
  `inlineData` decoding, the walk to the next candidate on each rejection class, the
  cooldown, an exhausted chain returning `None`, the deadline cutting the walk short, and
  that the import still cannot fail because of any of it. All without network access, as
  `DESIGN.md` §12 requires.
- `DESIGN.md` §3 (configuration table, picture stage), `.env.example` and `README.md`.
- Money: this is the first stage of the service that spends real credit per import. The
  design records the measured per-picture price and what the $5 buys, and the rate limit
  (`RATE_LIMIT_PER_HOUR`, 20) stays the ceiling on how fast it can be spent.
