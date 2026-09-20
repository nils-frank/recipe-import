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

- [ ] 3.1 Create `src/image_chain.py` with `candidates()`, `mark_exhausted()`,
  `mark_unknown()`, `reset()`, the `_now = time.monotonic` seam and a `threading.Lock`,
  keyed by the `provider:model` pair; verify `candidates()` returns the configured order
  on a fresh module.
- [ ] 3.2 Implement cooldown filtering; verify in `tests/test_image_chain.py` that an
  exhausted candidate is absent before its deadline and present after, using a patched
  `_now` and no sleeping.
- [ ] 3.3 Implement `mark_unknown` as process-lifetime removal; verify the candidate never
  reappears regardless of the clock.
- [ ] 3.4 Handle the empty-chain case as a normal state rather than an exception; verify a
  test asserts an empty list.
- [ ] 3.5 Reset the chain state per test the way `frische_modellkette` does for the text
  chain; verify by adding the fixture to `tests/conftest.py` and asserting two tests in a
  row each start from the configured order.

## 4. The Gemini fetcher

- [ ] 4.1 Add `_fetch_gemini(prompt, candidate)` to `src/image.py`: `POST
  {base}/v1beta/models/{model}:generateContent`, key in `x-goog-api-key`, the existing
  prompt as a single text part, and `generationConfig` carrying
  `responseModalities: ["IMAGE"]` plus `imageConfig.aspectRatio: "1:1"`; verify a test
  asserts the exact URL, header name and body shape against a stubbed `requests.post`.
- [ ] 4.2 Decode the answer from the first part carrying `inlineData`, ignoring text parts
  that precede it; verify tests for: image part only, text part then image part, text part
  only, no candidates, and base64 that does not decode.
- [ ] 4.3 Route the existing `pollinations` and `openai` fetchers through the same
  per-candidate signature so all three take a candidate instead of reading
  `config.IMAGE_PROVIDER`; verify the existing Pollinations and OpenAI tests in
  `tests/test_image.py` still pass with no change to their assertions about the request.

## 5. The chain walk in `image.generate`

- [ ] 5.1 Turn `generate` into a walk over `image_chain.candidates()` with a local
  per-import skip set, stopping at the first usable picture and returning `None` when the
  chain is used up; verify a test where the first candidate fails and the second returns
  the fixture JPEG.
- [ ] 5.2 Implement the failure classification table from `design.md` (exhausted, unknown
  model, unusable), reusing `llm._UNKNOWN_MODEL_MARKERS`; verify one test per bucket
  asserting which chain-state call was made and that the walk continued.
- [ ] 5.3 Keep the existing guards (empty body, `MAX_IMAGE_BYTES`, `llm._image_mime`) as
  the unusable bucket for every provider; verify the existing oversize and bad-magic-bytes
  tests still pass and now continue to the next candidate.
- [ ] 5.4 Enforce `IMAGE_DEADLINE_SECONDS` as one monotonic deadline for the whole stage,
  with each request timeout being the smaller of the provider timeout and the time left;
  verify a test with a patched clock asserts that the third candidate is never contacted
  once the budget is gone.
- [ ] 5.5 Make one call per candidate with no retry inside a candidate; verify a test
  counts exactly one request per candidate across a full failing walk.
- [ ] 5.6 Log the candidate that produced the picture, and log the reason on every skip;
  verify a test asserts no log line and no exception text contains an API key.

## 6. Behaviour of the stage as a whole

- [ ] 6.1 Verify with a test through `app._attach_image` that an import stays successful,
  `done` and untagged when every candidate fails, including a transport error, a `429` and
  an unusable answer in the same walk.
- [ ] 6.2 Verify with a test that a picture from a fallback candidate is uploaded and
  tagged `ki-bild` exactly like one from the head candidate.
- [ ] 6.3 Verify with a test that a recipe that already has an image, and a repeated import
  of a known source, contact no candidate at all.
- [ ] 6.4 Verify with a test that `IMAGE_ENABLED=false` short-circuits before the chain is
  even read.
- [ ] 6.5 Verify with a test that Pollinations is called without an `Authorization` header
  when no Pollinations token is configured, while a Gemini candidate in the same chain is
  called with the key.

## 7. Documentation

- [ ] 7.1 Update `DESIGN.md` §3 for the picture stage: the chain, the three provider forms,
  the new values and the money note; verify the configuration table lists every new
  variable with its default.
- [ ] 7.2 Update `.env.example` with `IMAGE_MODEL_CHAIN`, `IMAGE_MODEL_COOLDOWN_SECONDS`
  and `IMAGE_DEADLINE_SECONDS`, and correct the "the Gemini key cannot generate images"
  paragraph to what task 1 measured; verify no stale claim about the free tier remains.
- [ ] 7.3 Update the picture-stage paragraph in `README.md` to describe the chain and the
  free floor in two sentences; verify it names no model id that task 1 did not prove.

- [ ] 7.4 Update the runbook in the operations repository
  (`~/Documents/github/homelab/setup-recipe-import.md`) with the new variables and the fact
  that the stage now spends credit, and commit it there, not here; verify the runbook names
  the same defaults as `.env.example`.

## 8. Gate

- [ ] 8.1 Run `ruff check .` and `pytest`; verify both are clean, with no test needing
  network access.
- [ ] 8.2 Deploy to the host, import one recipe that has no picture, and verify in Mealie
  that the picture came from the head candidate, carries no watermark, and that the log
  names that candidate.
- [ ] 8.3 Force the fallback once on the deployment (a chain whose head is a model id that
  does not exist) and verify the import still produces a picture from the free provider,
  the log names both the skip and the candidate that delivered, and the import reports
  success.
