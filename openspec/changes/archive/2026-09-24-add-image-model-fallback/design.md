# Design

## Context

See `proposal.md` - Why, and `specs/recipe-image/spec.md` for the requirements. The
constraints that shape the approach:

- The whole picture stage is one function, `image.generate(name, ingredients) -> bytes |
  None` (`src/image.py`), called from `app._attach_image` after `store.finish()`. It never
  raises; every failure path logs and returns `None`. The call site reads bytes or `None`
  and needs no change for this work.
- `image.py` already holds two provider forms that cannot be translated into each other:
  `pollinations` (a `GET` whose body *is* the image) and `openai` (`POST
  /images/generations`, JSON both ways, `data[0].b64_json` or `data[0].url`). Adding a
  third is adding a third fetcher, not generalising the two.
- The text stage solved the same problem in A21: `llm.py` holds transport, schema and
  parsing, `model_chain.py` holds order, clocks and cooldowns, with `_now =
  time.monotonic` as the test seam and a `threading.Lock` because imports run in FastAPI's
  thread pool. This change follows that split rather than inventing a second shape.
- Measured against the deployed key on 2026-09-20 and recorded in `src/config.py`:
  `/images/generations` on the Gemini OpenAI-compatible surface maps to `predict`, which
  none of its 58 models serve (`404`). The image models answer on the native surface via
  `generateContent`, and at that time all of them answered `429
  "generate_content_free_tier_... limit: 0"` - `gemini-2.5-flash-image`,
  `gemini-3-pro-image(-preview)`, `gemini-3.1-flash-image(-preview)`,
  `gemini-3.1-flash-lite-image`, `nano-banana-pro-preview`. A key from a freshly created
  project of the same account answered the same, so the zero was at account level.
- Measured for Pollinations the same day: `200 image/jpeg`, 768x768, 46-73 KB, 35-46 s per
  picture, no account and no key; the `pollinations.ai` watermark stays even with a free
  token; two concurrent requests give `429`.
- **Measured against the credited key on 2026-09-20 (task 1), run on the deploy host so
  the key never left it.** The $5 of credit reverses the measurement above: every image
  model now answers `200` with a real picture. The provider lists seven image models, all
  through `generateContent`; the `veo-*` entries are video and use `predictLongRunning`.
  One call each, with this service's own prompt and `responseModalities: ["IMAGE"]`:

  | model | answer | picture | image tokens | price per picture |
  | --- | --- | --- | --- | --- |
  | `gemini-3-pro-image` | `200` in 15.3 s | `image/jpeg`, 1408x768, 897 KB | 1120 | $0.134 |
  | `nano-banana-pro-preview` | `200` in 15.3 s | `image/jpeg`, 1408x768, 747 KB | 1120 | $0.134 |
  | `gemini-3.1-flash-image` | `200` in 8.6 s | `image/jpeg`, 1408x768, 914 KB | 1120 | $0.067 |
  | `gemini-3.1-flash-lite-image` | `200` in 3.2 s | `image/jpeg`, 1408x768, 899 KB | 1120 | $0.034 |
  | `gemini-2.5-flash-image` | `200` in 5.7 s | `image/png`, 1024x1024, 2.19 MB | 1290 | $0.039 |

  The prices are the measured image-token counts against the published paid-tier rates
  ($120, $60 and $30 per million image tokens), not a per-picture figure read off a page:
  1120 tokens at $120 per million is $0.1344. So the $5 buys about **37 pictures from the
  pro model**, 74 from flash, or 148 from flash-lite.

  `nano-banana-pro-preview` returned the same size, the same token count and the same
  timing as `gemini-3-pro-image`: it is the preview id of that model, so the stable id is
  the one worth pinning. `gemini-2.5-flash-image` is marked deprecated, is the only one
  answering PNG, and is the only one whose answer is over 2 MB.

  Every picture was a photograph of the dish with no text, no logo and no watermark. Two
  were looked at: the pro and the flash one, both plausible food photography.

  `generationConfig.imageConfig.aspectRatio: "1:1"` is accepted and returns 1024x1024 for
  **the same 1120 tokens**, so a square picture costs the same as the default 1408x768.
- Tests run "ohne jeden Netzzugriff" (`DESIGN.md` §12). Every clock and every HTTP call
  needs a seam, and `tests/conftest.py` keeps the stage switched off by default.

## Goals / Non-Goals

**Goals:**

- One ordered chain, walked once per import, that prefers the paid Gemini image models and
  ends at the free provider, so a spent credit degrades the picture instead of removing
  it.
- The credit running out, and being topped up again, both happen without a configuration
  change and without a deploy.
- A hard ceiling on what one import can cost in money (one call per candidate) and in time
  (one budget for the whole stage).
- No change to the guarantee that this stage can never fail an import, and no change to
  anything the user sees.

**Non-Goals:**

- Spend accounting. The service will not track dollars, count images against a budget, or
  ask the provider for a balance. `RATE_LIMIT_PER_HOUR` (20) already bounds the rate, and
  an exhausted candidate is reported by the provider itself.
- Automatic discovery of newer image models. See `proposal.md` - an image probe costs an
  image.
- Parallel or speculative calls to several candidates.
- Changing the prompt, the picture size, the `ki-bild` tag, or the Mealie upload.

## Decisions

### A chain of `provider:model` entries, not a chain of providers

`IMAGE_MODEL_CHAIN` is a comma-separated list of `provider:model` entries, parsed in
`config.py` into a list of pairs, order preserved, duplicates dropped - the same shape
`_split_chain` gives `LLM_MODEL_CHAIN`.

Alternatives considered. *A chain of providers only, each with its own configured model*:
cannot express "the pro model first, the cheaper model of the same provider second", which
is exactly the middle of the intended default. *Two parallel lists (providers, models)*:
two lists that must stay the same length is a configuration error waiting to happen.
*A structured value (JSON, YAML) in the environment*: every other value in `.env` is a
scalar or a comma list; a JSON blob would be the only one needing a parser and error
wording of its own.

An entry whose provider is not one of the three known names is dropped with a warning
rather than failing the start, following the existing `IMAGE_PROVIDER` rule: the picture
stage must never cost a start. An entry with no colon is read as a model on the default
provider for backwards friendliness, with a warning.

Default, from the measurements in Context:

```
gemini:gemini-3-pro-image, gemini:gemini-3.1-flash-image, pollinations:sana
```

The pro model first because it is the best picture for $0.134 and 15 seconds, and because
the free floor underneath means a spent credit costs nothing but quality.
`gemini-3.1-flash-image` keeps the middle slot: the only case it serves is the pro model
being briefly unavailable while the account still has credit, which is exactly what A21
measured for the text models, and at $0.067 it halves the price of that case. The preview
id `nano-banana-pro-preview` is left out as a duplicate of the pro model, and the
deprecated `gemini-2.5-flash-image` is left out for being deprecated.

### Backwards compatibility follows the `LLM_MODEL` rule

`IMAGE_MODEL_CHAIN` set explicitly wins outright. Otherwise the default chain applies, and
an **explicitly set** `IMAGE_PROVIDER`/`IMAGE_MODEL` pair is moved to the front of it, so
a deployment that pinned Pollinations keeps getting Pollinations first and gains the
Gemini entries only underneath. The pair's own default value is not an instruction: the
defaults alone leave the chain in its configured order. This is the rule
`config._resolve_model_chain` already implements for text, and the reason it is worth
copying is that a deployment's `.env` is the one thing this change cannot test.

### Credentials resolve per provider, not per candidate

A table in `config.py` maps each provider to its base URL and key:

| provider | base URL | key |
| --- | --- | --- |
| `gemini` | `LLM_BASE_URL` with a trailing `/openai` removed, which is the native root including its `/v1beta` version segment | `LLM_API_KEY` |
| `openai` | `LLM_BASE_URL` | `LLM_API_KEY` |
| `pollinations` | `https://image.pollinations.ai` | empty |

`IMAGE_BASE_URL` and `IMAGE_API_KEY` keep their current meaning and override the entry of
the provider named by `IMAGE_PROVIDER` - that is, the one the deployment pinned. They do
not apply to the other providers, because a single override applied to every provider is
how the text model's key ends up at a different company, which the spec forbids.

The `gemini` row reuses the text key deliberately: same provider, same account, same
credit. The derivation from `LLM_BASE_URL` keeps one address configured instead of two
that can disagree; a deployment pointing `LLM_BASE_URL` at a proxy without an `/openai`
suffix gets that address unchanged, and can still set `IMAGE_BASE_URL` explicitly.

### The Gemini fetcher speaks the native surface

`POST {base}/models/{model}:generateContent` with `x-goog-api-key: <key>` and a body
carrying the existing one-sentence prompt as a single text part, plus

```json
{"generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "1:1"}}}
```

The picture comes back as base64 in a part's `inlineData.data`, with its type in
`inlineData.mimeType`. The response may also carry text parts, and may carry no image part
at all when the model answers in words instead - that case is an unusable answer, not an
error, and the chain moves on. All of this is the measured shape, not a read of the
documentation.

Alternative considered: keep using the OpenAI-compatible surface and wait for Google to
map `/images/generations`. Measured `404` on 2026-09-20; waiting is not a design.

### The picture is asked for square

`aspectRatio: "1:1"` gives 1024x1024 instead of the default 1408x768, for the same 1120
image tokens and the same price. Mealie's tile view is what these pictures exist for, and
the stage's other provider already returns 768x768, so a square picture is both the shape
that fits and the shape that keeps the two providers comparable. A 1408x768 picture would
be cropped by the tile anyway, which is paying full price for pixels that are thrown away.

The existing decoding guards stay in front of the result for every provider: non-empty,
at most `MAX_IMAGE_BYTES`, and `llm._image_mime` must recognise the bytes.

### No retry within a candidate; the next candidate is the retry

`llm._post` retries once on a transient status before moving on. The picture stage does
not: an image call is the most expensive and slowest request this service makes, the
answer is worthless if it is late, and the next candidate is both cheaper and more likely
to answer. One call per candidate, no exceptions, which also makes the money ceiling per
import trivially equal to the chain length.

### Failure classification

Per candidate, the answer falls into exactly one of three buckets:

| bucket | recognised by | consequence |
| --- | --- | --- |
| exhausted | HTTP 429, or 402/403 whose body names quota, credit, billing or `RESOURCE_EXHAUSTED` | `mark_exhausted`, skip for the cooldown, next candidate |
| unknown model | HTTP 404, or a 400 carrying one of the existing `llm._UNKNOWN_MODEL_MARKERS` | `mark_unknown`, out for the process lifetime, next candidate |
| unusable | everything else: transport error, other status, wrong content type, no image part, undecodable base64, empty body, oversized body, unrecognised magic bytes | skip for this import only, next candidate |

The markers are shared with `llm.py` rather than copied, so a name the provider retires is
recognised the same way in both stages. Pollinations answering `429` for two concurrent
imports lands in `exhausted`, which is right: for the next hour that provider is busy, and
the paid candidates above it are the ones that should be tried first anyway.

### Chain state lives in `image_chain.py`

`candidates()`, `mark_exhausted(candidate)`, `mark_unknown(candidate)`, `reset()`, a
module-level `_now = time.monotonic` and a `threading.Lock`, mirroring `model_chain.py`
down to the names so that a reader who knows one knows the other. The key is the
`provider:model` pair, so the same model name on two providers is two candidates. What one
import has already tried is deliberately not module state: `generate` reads the candidate
list once and walks that snapshot, which by construction asks each candidate at most once
and forgets it at the end of the walk.

State is in-process only. A restart begins at the configured order again, which is the same
trade-off A21 already accepted and documented.

### One deadline for the stage, per-provider timeouts underneath

`IMAGE_DEADLINE_SECONDS` (default 150) is turned into a monotonic deadline when `generate`
starts. Before each candidate the remaining time is computed; if it is not enough for that
candidate's own timeout, the walk stops and the import completes without a picture. Each
call carries its provider's full timeout rather than a truncated one: a paid call that is
not allowed to finish is money for nothing, so the stage would rather end without a
picture. Checking before each call is what keeps the stage inside the budget, since a call
only starts when its whole timeout still fits.

150 seconds is measurement plus headroom. Measured per picture: the Gemini candidates
answered in 3.2 to 15.9 s, Pollinations in 35-46 s. A walk that fails at both Gemini
candidates and succeeds at Pollinations therefore costs about a minute, well inside the
budget, and the budget only bites when candidates hang rather than answer. `app._publish`
holds the push notification until the stage returns, so a chain that could burn three
90-second timeouts would make a successful import feel broken. The Gemini per-request
timeout is 60 s against a measured 16 s worst case; Pollinations keeps its 90 s.

### No spend accounting, no balance check

Rejected: a counter in SQLite, a monthly cap, a "credit low" push. $5 of credit with a
20 imports per hour ceiling and a free floor underneath does not justify a control plane,
and the provider already reports the only fact that matters ("exhausted") at the moment it
becomes true. `IMAGE_ENABLED=false` remains the one-line retraction.

### The tag does not change

A generated picture carries `ki-bild` regardless of which candidate produced it. The
candidate is named in the log line and nowhere else. A tag per provider would put
provider names into a user's Mealie library for no decision the user makes there.

## Risks / Trade-offs

- **The credit is finite and small.** $5 is about 37 pictures from the pro model, so a
  busy month spends it → the chain's whole point: the free floor catches it, and the
  cheaper middle candidate is one edit away from becoming the head if the price matters
  more than the picture.
- **Silent quality swing.** When the credit runs out, pictures go back to 768x768 with a
  watermark and nobody is told → the log line names the candidate, and this is a
  deliberate trade: a push notification per provider switch would be noise on every import
  once the credit is gone.
- **Cost per import rises from zero to cents.** → One call per candidate, the chain stops
  at the first usable picture, `RATE_LIMIT_PER_HOUR` bounds the rate, repeated imports of
  a known source generate nothing, and `IMAGE_ENABLED=false` switches the stage off.
  Worst case per import is one paid call plus one free call.
- **Longer waits before the push notification.** Two failing paid candidates in front of a
  40-second Pollinations call is a slower import than today → the deadline bounds it, and
  the exhausted-cooldown means the failing candidates are skipped entirely from the second
  import onward.
- **The text key now travels to a second endpoint of the same provider.** → Same company,
  same account, same credential that already goes there for text; the per-provider table
  is what keeps it from travelling anywhere else, and a test asserts Pollinations is called
  without an `Authorization` header when no Pollinations token is configured.
- **A Gemini model that answers with text instead of a picture** (a safety response, or a
  chatty answer) → classified unusable, chain continues, one test covers exactly that
  answer shape.
- **Default chain names a model this account cannot serve** → every id in the default was
  called once against the credited key and answered with a picture (Context), and an
  unknown model is skipped at runtime rather than failing.

## Migration Plan

Configuration only; no data, no schema, no container change.

1. Deploy with `.env` untouched. The default chain applies, Gemini is tried first, and any
   failure lands on Pollinations, which is what the deployment does today.
2. Watch the log line naming the candidate for the first few imports, and check one recipe
   in Mealie for a watermark-free picture.
3. Roll back in one line without a redeploy of config semantics: `IMAGE_MODEL_CHAIN=`
   `pollinations:sana` restores exactly today's behaviour, and `IMAGE_ENABLED=false`
   removes the stage.

**State on 2026-09-24: the credit is gone and is not being topped up.** The account answers
`402 "Your prepayment credits are depleted"` for every model. The deployed service
therefore runs the chain as designed and lands on Pollinations every time, which is the
behaviour this change exists for. Two deliberate decisions follow:

- The default chain keeps Gemini at the head rather than being pinned to
  `pollinations:sana` on the host. A `402` is refused instantly and costs nothing, the
  cooldown holds it to two rejected calls per hour per process, and leaving it in place
  means the paid models come back by themselves the moment credit exists - no `.env` edit,
  no deploy, nobody having to remember.
- The head-candidate verification is deferred, not skipped. When there is credit again:
  import one recipe that has no picture, confirm the log reads
  `Bild ... von gemini:gemini-3-pro-image erzeugt`, and look at the picture in Mealie for
  the absence of a watermark.

Out of this change's scope but found by it: with the credit gone, the **text** stage also
answers `402`, and `llm._post` treats that as a hard failure rather than a reason to move
down its own chain. Every import therefore fails outright instead of being parked by the
retry queue. That belongs to the `llm-model-fallback` capability and needs its own
change.

## Open Questions

- Whether one cooldown fits both reasons a candidate reports itself spent. A provider busy
  for a minute and a credit gone for the rest of the month are the same signal here, and
  both are skipped for an hour. The cost of getting it wrong is one rejected call per hour,
  which is free, so a second value can be added later without touching the specs.
- How the pictures compare once a few real recipes have gone through, rather than one probe
  each. The flash picture measured usable, which is why it keeps the middle slot, but only
  imports of real recipes will show whether the pro model is worth twice the price here.
  Either answer is a one-line change to the default chain.
