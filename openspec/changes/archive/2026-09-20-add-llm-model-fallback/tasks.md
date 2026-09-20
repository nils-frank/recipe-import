# Tasks

## 1. Verify the provider's real model list

- [x] 1.1 With the deployed `LLM_API_KEY`, call `GET {LLM_BASE_URL}/models` and record in
  `design.md` (Context) which `gemini-*-flash` models actually exist, including whether
  ids carry a `models/` prefix; verify by pasting the filtered id list into the design.
- [x] 1.2 Send one schema-enforced probe call (`response_format: json_schema`, strict) to
  each candidate found in 1.1 and note which answer `200` and which answer `429` with a
  zero quota; verify the outcome is recorded per model.
- [x] 1.3 Correct the default chain in the proposal, spec and design to the models 1.1/1.2
  proved to exist, keeping newest-first order; verify no unverified model name remains in
  the defaults.

## 2. Configuration

- [x] 2.1 Add `LLM_MODEL_CHAIN`, `LLM_MODEL_COOLDOWN_SECONDS`, `LLM_MODEL_AUTODISCOVER`,
  `LLM_MODEL_PATTERN` and `LLM_MODEL_REFRESH_SECONDS` to `src/config.py` with the defaults
  from `design.md` and German comments in the style of the surrounding file; verify by
  importing `config` in a test and asserting each default.
- [x] 2.2 Implement the chain resolution rule from `design.md` (explicit
  `LLM_MODEL_CHAIN` wins; otherwise the default list with a non-default `LLM_MODEL` moved
  to the front); verify with `tests/test_config_model_chain.py` covering: unset, explicit
  chain, whitespace and empty entries, and a pinned `LLM_MODEL` outside the default list.
- [x] 2.3 Keep `LLM_MODEL` defined and unchanged in meaning for logs; verify an existing
  `.env` with only `LLM_MODEL` set still yields that model as chain head.

## 3. Chain state module

- [x] 3.1 Create `src/model_chain.py` with `candidates()`, `mark_exhausted()`,
  `mark_unknown()`, `head()`, the `_now = time.monotonic` seam and the `threading.Lock`;
  verify `candidates()` returns the configured order on a fresh module.
- [x] 3.2 Implement cooldown filtering; verify in `tests/test_model_chain.py` that a
  model marked exhausted is absent from `candidates()` before its deadline and present
  after, using a patched `_now` and no sleeping.
- [x] 3.3 Implement `mark_unknown` as process-lifetime removal; verify the model never
  reappears in `candidates()` regardless of the clock.
- [x] 3.4 Handle the empty-chain case: `candidates()` returning nothing is a normal state
  the caller must handle; verify a test asserts an empty list rather than an exception.

## 4. Chain traversal in `llm._post`

- [x] 4.1 Restructure `_post` into the outer candidate loop plus the existing inner
  two-attempt loop, setting `payload["model"]` per candidate, and add the optional
  `model: str | None` argument that pins one model for the probe; verify the existing
  `tests/` suite still passes unchanged.
- [x] 4.2 Implement the status table from `design.md`: second `429` marks exhausted and
  moves to the next model; second `502/503/504` moves to the next model without a cooldown
  mark; `500` keeps today's behaviour; `404`/model-not-found `400` marks unknown and moves
  on. Verify with `tests/test_llm_fallback.py` using a fake `requests.post`, one test per
  row, including that a `503`-hit model is eligible again on the next call.
- [x] 4.3 Raise `LlmOverloadedError` when the chain is used up by 429s or by 5xx, and `LlmError`
  when every candidate was unknown; verify both messages name the models tried and that
  `app._describe_source_error` still maps them to the unchanged §7 wording.
- [x] 4.4 Verify the schema-retry budget is untouched: a call whose first answer fails
  validation and whose retry hits a 429 twice is re-sent to the next model and still gets
  exactly one schema retry in total (test in `tests/test_llm_fallback.py`).
- [x] 4.5 Log every model switch at warning level with the old model, the new model and
  the status that caused it, and never log a response header or key; verify with `caplog`
  in the fallback tests.
- [x] 4.6 Verify `naming.make_name` inherits the chain and still never raises: a test with
  every model answering 429 asserts `None` and a warning, not an exception.

## 5. Discovery, probe and adoption

- [x] 5.1 Implement the model-list read in `model_chain.refresh()` (single attempt, short
  timeout, `models/` prefix stripped, `LLM_MODEL_PATTERN` filter, numeric `(major, minor)`
  descending sort); verify with a fixture response that `3.10` sorts above `3.9`.
- [x] 5.2 Implement the configured-chain cross-check that logs and skips configured models
  the provider does not list; verify a test asserts the skip and the log line.
- [x] 5.3 Implement the probe: one `_post` with a pinned model, a one-field strict schema
  and a 30 s timeout; adoption only on a schema-conformant answer. Verify tests for
  success, non-200, timeout and schema mismatch, and that a rejected model is not probed
  again within the same refresh run.
- [x] 5.4 Implement adoption: a newer, probed model becomes the head, the previous chain
  follows unchanged; verify `candidates()` order after adoption in a test.
- [x] 5.5 Send exactly one `ha_notify.notify` on a changed head, naming old and new model,
  and stay silent on a later run that finds the same head; verify with a fake notify in
  two consecutive refresh runs.
- [x] 5.6 Honour `LLM_MODEL_AUTODISCOVER=false`: no model-list request, no probe, no
  notification; verify a test asserts zero HTTP calls.
- [x] 5.7 Make every discovery failure non-fatal (connection error, non-200, unreadable
  body, empty list): log and keep the current chain; verify one test per failure shape.

## 6. Wiring into the service

- [x] 6.1 Start the refresh task in `app.lifespan` (one run at startup, then every
  `LLM_MODEL_REFRESH_SECONDS`, executed through `asyncio.to_thread`), and cancel it
  cleanly on shutdown; verify startup does not block on it via a test that makes the model
  list hang and still gets `200` from `/healthz`.
- [x] 6.2 Verify the image stage is untouched: a test asserts `image.py` still uses
  `config.IMAGE_MODEL` while the text chain has switched models.

## 7. End-to-end verification

- [x] 7.1 End-to-end test through the import endpoint with a fake provider whose first
  model always answers 429: assert the recipe is created in the fake Mealie, the success
  notification is sent, and the fallback model was the one that answered.
- [x] 7.2 End-to-end test with every model answering 429: assert the import ends `failed`
  with the unchanged "Das Sprachmodell ist gerade überlastet. Bitte später erneut teilen."
  notification.
- [x] 7.3 Run the full suite plus `ruff check .` and verify both are clean.
  Nachgeholt, nachdem `add-linting` den Regelsatz festgelegt hat (`pyproject.toml`,
  `ruff==0.16.8`): 296 Tests bestanden, `ruff check .` meldet "All checks passed!".
  Die drei `except Exception` in `src/model_chain.py` tragen seither dasselbe
  `# noqa: BLE001` mit Begründung wie die gleichartigen Stellen in `app.py` - das
  Muster "diese Stufe darf nie werfen" ist damit benannt statt stillschweigend.

## 8. Documentation

- [x] 8.1 Update `DESIGN.md` §3: replace the "fest verdrahtet, kein wandernder Alias" rule
  for the text model with the chain, state why it was reversed, and add the five new
  variables to the configuration table; verify the table matches `src/config.py`.
- [x] 8.2 Update `DESIGN.md` §7 so the overload row reads as "every model in the chain is
  exhausted" and add the unknown-model case; verify the wording matches
  `app._describe_source_error` literally.
- [x] 8.3 Update `.env.example` and `README.md` with the new variables, the default chain
  and the one-variable rollback (`LLM_MODEL_AUTODISCOVER=false`); verify by diffing the
  variable names against `src/config.py`.
- [x] 8.4 Document in `README.md` how to see which models the provider currently offers
  (`GET {LLM_BASE_URL}/models`) and that the service adopts newer ones by itself; verify
  the documented command is the one `model_chain.refresh()` uses.
