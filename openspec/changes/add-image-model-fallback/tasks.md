# Tasks

## 1. Prove the credited key can generate images

Run on the deploy host, where `LLM_API_KEY` lives; the key never leaves it and never
appears in a file of this repository.

- [x] 1.1 Call `GET {native base}/v1beta/models` with the deployed key and record in
  `design.md` (Context) every id that generates images, including whether ids carry the
  `models/` prefix; verify the filtered id list is pasted into the design.
- [x] 1.2 Send one real generation call per candidate id from 1.1 through
  `:generateContent` with the existing picture prompt, and record per id: HTTP status,
  whether a part with `inlineData` came back, the MIME type, byte size, pixel size,
  wall-clock duration and whether any watermark is burned in; verify every id from 1.1 has
  a row.
- [x] 1.3 Record the per-picture price of the ids that worked and how many pictures the $5
  buys at that price; verify the number is in `design.md` and not an estimate carried over
  from a marketing page.
- [x] 1.4 If no id produced a picture, stop the change here: record the answer in
  `design.md` (Risks, first entry), leave the stage as it is, and report back rather than
  building a chain whose head cannot work. **Did not fire:** all five ids answered `200`
  with a picture, so the change continues.
- [x] 1.5 Replace the proposed default chain in `proposal.md`, `design.md` and
  `specs/recipe-image/spec.md` with the ids 1.2 proved, best first, dropping any entry
  1.2 showed to be unusable; verify no unverified model id remains anywhere in the change.

## 2. Configuration

- [x] 2.1 Add `IMAGE_MODEL_CHAIN`, `IMAGE_MODEL_COOLDOWN_SECONDS` and
  `IMAGE_DEADLINE_SECONDS` to `src/config.py` with the defaults from `design.md` and
  German comments in the style of the surrounding file; verify by asserting each default
  in `tests/test_config_image_chain.py`.
- [x] 2.2 Implement chain parsing: comma list of `provider:model`, whitespace stripped,
  empty entries dropped, duplicates dropped, order preserved, an unknown provider dropped
  with a warning, an entry without a colon read as a model on the default provider with a
  warning; verify each of those six cases in `tests/test_config_image_chain.py`.
- [x] 2.3 Implement the resolution rule from `design.md`: an explicit `IMAGE_MODEL_CHAIN`
  wins, otherwise the default chain with an explicitly set `IMAGE_PROVIDER`/`IMAGE_MODEL`
  pair moved to the front; verify with tests for unset, explicit chain, pinned pair inside
  the default chain and pinned pair outside it.
- [x] 2.4 Add the per-provider base URL and key table, with the `gemini` base derived from
  `LLM_BASE_URL` by removing a trailing `/openai`, and `IMAGE_BASE_URL`/`IMAGE_API_KEY`
  overriding only the provider named by `IMAGE_PROVIDER`; verify a test asserts the
  Pollinations entry carries an empty key when only `LLM_API_KEY` is set.
- [x] 2.5 Keep `IMAGE_ENABLED`, `IMAGE_PROVIDER`, `IMAGE_MODEL`, `IMAGE_BASE_URL` and
  `IMAGE_API_KEY` defined and unchanged in meaning; verify the existing
  `tests/test_config_image.py` still passes untouched.

## 3. Chain state module

- [x] 3.1 Create `src/image_chain.py` with `candidates()`, `mark_exhausted()`,
  `mark_unknown()`, `reset()`, the `_now = time.monotonic` seam and a `threading.Lock`,
  keyed by the `provider:model` pair; verify `candidates()` returns the configured order
  on a fresh module.
- [x] 3.2 Implement cooldown filtering; verify in `tests/test_image_chain.py` that an
  exhausted candidate is absent before its deadline and present after, using a patched
  `_now` and no sleeping.
- [x] 3.3 Implement `mark_unknown` as process-lifetime removal; verify the candidate never
  reappears regardless of the clock.
- [x] 3.4 Handle the empty-chain case as a normal state rather than an exception; verify a
  test asserts an empty list.
- [x] 3.5 Reset the chain state per test the way `frische_modellkette` does for the text
  chain; verify by adding the fixture to `tests/conftest.py` and asserting two tests in a
  row each start from the configured order.

## 4. The Gemini fetcher

- [x] 4.1 Add `_fetch_gemini(prompt, candidate)` to `src/image.py`: `POST
  {base}/v1beta/models/{model}:generateContent`, key in `x-goog-api-key`, the existing
  prompt as a single text part, and `generationConfig` carrying
  `responseModalities: ["IMAGE"]` plus `imageConfig.aspectRatio: "1:1"`; verify a test
  asserts the exact URL, header name and body shape against a stubbed `requests.post`.
- [x] 4.2 Decode the answer from the first part carrying `inlineData`, ignoring text parts
  that precede it; verify tests for: image part only, text part then image part, text part
  only, no candidates, and base64 that does not decode.
- [x] 4.3 Route the existing `pollinations` and `openai` fetchers through the same
  per-candidate signature so all three take a candidate instead of reading
  `config.IMAGE_PROVIDER`; verify the existing Pollinations and OpenAI tests in
  `tests/test_image.py` still pass with no change to their assertions about the request.

## 5. The chain walk in `image.generate`

- [x] 5.1 Turn `generate` into a walk over `image_chain.candidates()`, read once and
  walked as a snapshot so no skip set is needed, stopping at the first usable picture and returning `None` when the
  chain is used up; verify a test where the first candidate fails and the second returns
  the fixture JPEG.
- [x] 5.2 Implement the failure classification table from `design.md` (exhausted, unknown
  model, unusable), reusing `llm._UNKNOWN_MODEL_MARKERS`; verify one test per bucket
  asserting which chain-state call was made and that the walk continued.
- [x] 5.3 Keep the existing guards (empty body, `MAX_IMAGE_BYTES`, `llm._image_mime`) as
  the unusable bucket for every provider; verify the existing oversize and bad-magic-bytes
  tests still pass and now continue to the next candidate.
- [x] 5.4 Enforce `IMAGE_DEADLINE_SECONDS` as one monotonic deadline for the whole stage.
  **Deviation:** a call carries its provider's full timeout instead of the smaller of that
  and the time left, and the walk stops when the remaining time no longer covers the next
  candidate's timeout - a paid call that cannot finish is money for nothing. The stage
  still ends inside the budget, which is what the spec requires;
  verify a test with a patched clock asserts that the third candidate is never contacted
  once the budget is gone.
- [x] 5.5 Make one call per candidate with no retry inside a candidate; verify a test
  counts exactly one request per candidate across a full failing walk.
- [x] 5.6 Log the candidate that produced the picture, and log the reason on every skip;
  verify a test asserts no log line and no exception text contains an API key.

## 6. Behaviour of the stage as a whole

- [x] 6.1 Verify with a test through `app._attach_image` that an import stays successful,
  `done` and untagged when every candidate fails, including a transport error, a `429` and
  an unusable answer in the same walk.
- [x] 6.2 Verify with a test that a picture from a fallback candidate is uploaded and
  tagged `ki-bild` exactly like one from the head candidate.
- [x] 6.3 Verify with a test that a recipe that already has an image, and a repeated import
  of a known source, contact no candidate at all.
- [x] 6.4 Verify with a test that `IMAGE_ENABLED=false` short-circuits before the chain is
  even read.
- [x] 6.5 Verify with a test that Pollinations is called without an `Authorization` header
  when no Pollinations token is configured, while a Gemini candidate in the same chain is
  called with the key.

## 7. Documentation

- [x] 7.1 Update `DESIGN.md` §3 for the picture stage: the chain, the three provider forms,
  the new values and the money note; verify the configuration table lists every new
  variable with its default.
- [x] 7.2 Update `.env.example` with `IMAGE_MODEL_CHAIN`, `IMAGE_MODEL_COOLDOWN_SECONDS`
  and `IMAGE_DEADLINE_SECONDS`, and correct the "the Gemini key cannot generate images"
  paragraph to what task 1 measured; verify no stale claim about the free tier remains.
- [x] 7.3 Update the picture-stage paragraph in `README.md` to describe the chain and the
  free floor in two sentences; verify it names no model id that task 1 did not prove.

- [x] 7.4 Update the runbook in the operations repository
  (`~/Documents/github/homelab/setup-recipe-import.md`) with the new variables and the fact
  that the stage now spends credit, and commit it there, not here; verify the runbook names
  the same defaults as `.env.example`.

## 8. Gate

- [x] 8.1 Run `ruff check .` and `pytest`; verify both are clean, with no test needing
  network access.
- [x] 8.2 Deploy to the host and verify the stage against the live provider. Deployed and
  healthy; the head candidate could **not** be verified end to end and will not be for now:
  the account answers `402 "Your prepayment credits are depleted"` for every model, images
  and text alike, and the user has decided against topping the credit up. What was verified
  instead, on the deployed service: both Gemini candidates are recognised as exhausted, the
  picture comes from `pollinations:sana`, and the log names every skip and the candidate
  that delivered. The head-candidate check is written down in `design.md` (Migration Plan)
  for whenever credit exists again.
- [x] 8.3 Force the fallback once on the deployment and verify the import still produces a
  picture from the free provider, the log names both the skip and the candidate that
  delivered, and the import reports success. **No bogus model id was needed:** the account's
  prepay credit is depleted, so both Gemini candidates answer `402` for real. Verified on
  the host: both are marked exhausted for 3600 s and `pollinations:sana` returns a 64 KB
  JPEG.
