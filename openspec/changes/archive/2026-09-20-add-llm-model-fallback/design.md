# Design

## Context

See `proposal.md` - Why, and `specs/llm-model-fallback/spec.md` for the requirements. The
constraints that shape the approach:

- Every text-stage request goes through one function, `llm._post` (`src/llm.py`). It reads
  `config.LLM_MODEL` into the payload, and it already owns the two-attempt transient retry
  (`_RETRYABLE_STATUS = {429, 500, 502, 503, 504}`, two seconds apart). Extraction,
  image extraction and `naming.make_name` all call it, so a chain implemented there covers
  the whole text stage with no call-site change.
- `naming.py` must keep its promise never to raise (`DESIGN.md` §3 / A18). Whatever the
  chain does, an exhausted chain has to surface as the exception classes that exist today.
- The service has no background workers except FastAPI `BackgroundTasks` and the tasks
  started in `app.lifespan`. `requests` is synchronous and imports run in background tasks,
  so chain state is touched from several threads.
- Tests run "ohne jeden Netzzugriff" (`DESIGN.md` §12). Anything time-based or
  network-based needs a seam: an injectable clock and a single place that performs the
  model-list request.
- The provider is Gemini's OpenAI-compatible surface. Its model list is
  `GET {LLM_BASE_URL}/models`, shaped `{"data": [{"id": "..."}]}`; ids may or may not
  carry a `models/` prefix depending on the surface.
- **Measured against the deployed key on 2026-09-20** (task 1.1/1.2, run on the deploy
  host so the key never left it). `GET {LLM_BASE_URL}/models` answers
  `200` with 58 entries, and **ids do carry the `models/` prefix** on the
  OpenAI-compatible surface (`models/gemini-3.8-flash`), so stripping it is required, not
  optional. The ids matching `^gemini-(\d+)\.(\d+)-flash$` after stripping are exactly:

  ```
  gemini-2.5-flash
  gemini-3.5-flash
  gemini-3.6-flash
  gemini-3.7-flash
  gemini-3.8-flash
  ```

  Neighbours the pattern deliberately excludes: `gemini-3-flash-preview` (no minor),
  `gemini-3.5-flash-lite`, `gemini-flash-latest` (the wandering alias), and the image,
  live, tts and transcribe variants.

  One schema-enforced probe per candidate (`response_format: json_schema`, strict,
  one-field schema) gave:

  | model | answer |
  | --- | --- |
  | `gemini-3.8-flash` | `200`, `{"ok":true}` |
  | `gemini-3.7-flash` | `503` "high demand" twice, then `200` on the third try |
  | `gemini-3.6-flash` | `429` "You exceeded your current quota" - **the model the service pins today is out of quota right now** |
  | `gemini-3.5-flash` | `200`, `{"ok":true}` |
  | `gemini-2.5-flash` | `404` "no longer available to new users ... use models/gemini-3.6-flash" |

  So the default chain `gemini-3.8-flash, gemini-3.7-flash, gemini-3.6-flash,
  gemini-3.5-flash` names four models that all exist; no unverified name remains. The
  measurement also confirms the premise of the change (`3.6` at `429` while `3.8` and
  `3.5` answer `200`) and the need for `mark_unknown` (a retired `2.5-flash` answers
  `404`, not `400`). A configured model that does not exist stays a normal case, not a
  configuration error.

- **`503` is per-model on this provider, not provider-wide.** In the same minute
  `gemini-3.7-flash` answered `503` twice while `gemini-3.8-flash` and `gemini-3.5-flash`
  answered `200`. See the decision below - this is measured, not assumed.
- `DESIGN.md` §3 states the opposite of automatic adoption: "`LLM_MODEL` bewusst fest
  verdrahtet statt als wandernder Alias ..., damit ein Modellwechsel eine bewusste
  Änderung ist." This change knowingly reverses that rule for the text model, which is
  why adoption is probed, logged, pushed, and switchable off in one variable.

## Goals / Non-Goals

**Goals:**

- One place that answers "which model do I call next, and which ones are currently out",
  so `llm.py` keeps holding only transport, schema and parsing.
- Chain traversal that is testable without network and without sleeping: the candidate
  order is a pure function of configuration plus a state map, and time enters through one
  injectable clock.
- A fallback path that changes nothing observable except that fewer imports fail: same
  prompts, same enforced schema, same single schema retry, same error classes and wording.

**Non-Goals:**

- No per-model prompt, temperature or token tuning. A fallback model gets exactly the same
  request body with a different `model` field.
- No persistence of chain state. Cooldowns live in memory; a restart starts at the head.
- No cost or quality accounting per model, and no automatic return to a "better" model
  mid-call.
- No change to the image stage, and no chain for `IMAGE_MODEL`.
- No provider abstraction layer. One provider, one list endpoint, one name pattern.

## Decisions

### A new module `src/model_chain.py` holds the chain, not `llm.py`

`llm.py` is already the longest module in the service and its docstring documents a
deliberately flat retry policy. Chain order, cooldown bookkeeping, discovery, version
parsing and the probe are a separate concern with its own state and its own clock, so they
go into `src/model_chain.py` with a small interface:

```python
def candidates() -> list[str]          # ordered, eligible models, head first
def mark_exhausted(model: str) -> None # 429 twice -> cooldown
def mark_unknown(model: str) -> None   # 404 / model-not-found -> out for this process
def refresh() -> None                  # one discovery run: list, order, probe, adopt
def head() -> str                      # current preferred model, for logging
```

Alternative considered: keeping everything in `llm.py` behind module-level globals.
Rejected because the discovery run has to call `_post` itself for the probe, and a module
that both owns the transport and drives a background refresh over that transport is hard
to test in isolation.

Alternative considered: moving fallback to a LiteLLM proxy, which `DESIGN.md` §3 already
names as the intended future for `LLM_BASE_URL`. Rejected for now: it is a second
container and a second configuration surface for one behaviour, and the chain here stays
useful behind a proxy later (the proxy would simply be handed a one-entry chain).

### `_post` loops over candidates; the existing two-attempt rule stays inside

`_post` becomes two nested loops. The outer loop walks `model_chain.candidates()`; the
inner loop is today's `for attempt in (1, 2)` against one model. The classification rule:

| answer | inner attempt 1 | inner attempt 2 |
| --- | --- | --- |
| 200 | done | done |
| 429 | sleep, retry same model | `mark_exhausted`, next model |
| 502/503/504 | sleep, retry same model | next model, **no** cooldown mark |
| 500 | sleep, retry same model | `LlmOverloadedError` |
| 404 / model-not-found 400 | `mark_unknown`, next model | - |
| other 4xx/5xx | `LlmError` | `LlmError` |

Keeping the first 429 on the same model preserves the case a per-minute rate limit covers:
a short burst limit clears in seconds, and switching models for it would walk the whole
chain during one busy minute. Only the second 429 is read as "this quota is gone".

`502/503/504` switch models but leave no mark. The Context measurement is the reason: in
one minute `gemini-3.7-flash` answered `503` "This model is currently experiencing high
demand" twice while `gemini-3.8-flash` and `gemini-3.5-flash` answered `200`. On this
provider that status names one model's load, not the account's or the infrastructure's, so
the next model is a real chance rather than a wasted call. No cooldown follows, because
load passes in seconds to minutes: the same model is eligible again on the very next call,
and parking it for an hour over a momentary spike would be the more expensive mistake.

`500` keeps today's behaviour and does not switch. It names no model-specific condition,
and without a measurement saying otherwise, walking the chain on it would be a guess.

A chain used up by `429` or by `502/503/504` still ends in `LlmOverloadedError`, so
`LlmOverloadedError` keeps meaning exactly what the in-flight `add-transient-retry-queue`
change expects - the queue simply sees it less often.

Exhausting the chain raises `LlmOverloadedError` with the last model's detail text, so
`app._describe_source_error` and the §7 wording stay untouched. An all-unknown chain
raises `LlmError` instead: that is a configuration fault, not "try later", and it should
read as a failure rather than invite the user to re-share.

`_post` gains one optional argument, `model: str | None`, used only by the probe to pin a
single model and bypass the chain.

### Cooldown is a dict of expiry times behind one lock and one clock

State is two module-level maps: `_cooldown_until: dict[str, float]` (monotonic deadline)
and `_unknown: set[str]`. `candidates()` filters the configured order by
`_cooldown_until.get(m, 0) <= _now()` and `m not in _unknown`. Mutation happens under a
`threading.Lock`, because background imports run concurrently in the FastAPI threadpool.

Time comes from a module-level `_now = time.monotonic` seam that tests replace - no
`freezegun`, no sleeping tests, in line with `DESIGN.md` §12.

Alternative considered: persisting cooldowns in SQLite next to `imports`. Rejected: the
cost of getting this wrong is one wasted call after a restart, and the `store` schema is
about imports, not provider bookkeeping.

Alternative considered: modelling Gemini's actual daily quota reset (midnight America/
Los_Angeles). Rejected as a guess about provider internals; a flat, configurable cooldown
(`LLM_MODEL_COOLDOWN_SECONDS`, default 3600) is honest about what it knows. If an hour
proves too short in practice, it is one environment variable.

### Discovery: one task in `lifespan`, `to_thread` for the blocking call

`app.lifespan` starts a single `asyncio` task that runs `model_chain.refresh()` once at
startup and then every `LLM_MODEL_REFRESH_SECONDS` (default 86400). `refresh` is
synchronous `requests` code, so the task calls it through `asyncio.to_thread` and never
blocks the event loop. Startup does not wait for it: the chain is usable from
configuration alone, and discovery only ever improves it.

`refresh` steps:

1. `GET {LLM_BASE_URL}/models` with the existing bearer key, short timeout, one attempt.
   Any failure is logged at warning level and returns - the chain stays as it is.
2. Read `data[].id`, strip a leading `models/`, keep ids matching `LLM_MODEL_PATTERN`
   (default `^gemini-(\d+)\.(\d+)-flash$`), and sort by the parsed `(major, minor)` tuple
   descending. Numeric parsing, not string comparison, or `3.10` would sort below `3.9`.
3. Cross-check the configured chain against the listed ids and log any configured model
   the provider does not list; those are skipped while the list says so. This is what
   catches a default that has been retired since, the way `gemini-2.5-flash` was,
   before an import does.
4. If the newest listed model is newer than the current head and has not already been
   probed and rejected, probe it (below). On success it becomes the head; the previous
   chain follows unchanged beneath it.
5. On a changed head: log old and new, and send exactly one `ha_notify.notify`. The
   adopted name is remembered so the next run is silent.

Alternative considered: discovery at import time instead of on a timer. Rejected: it puts
a second HTTP round trip and possibly a probe call in front of a user-visible import.

### The probe is one minimal schema-enforced call, not a plain ping

A model that exists and answers chat is not automatically usable here: the whole text
stage depends on `response_format: {type: json_schema, strict: true}`. The probe therefore
goes through `_post(model=<candidate>, schema=<one-field schema>, timeout=30)` with a
one-sentence prompt, and adoption requires a parseable answer matching that schema. A
probe failure of any kind (status, timeout, unparseable, schema mismatch) rejects the
model, logs it at warning level and records it so the same model is not probed again until
the next refresh.

This is the safety that makes automatic adoption acceptable against `DESIGN.md` §3: a
model is adopted only after it has demonstrated the exact contract the service relies on,
and the cost is one small call per newly seen model name.

### Configuration keeps `LLM_MODEL` meaningful

`config.py` resolves the chain once, at import:

- `LLM_MODEL_CHAIN` set - comma-separated, whitespace stripped, empties dropped, order
  preserved. That is the chain.
- `LLM_MODEL_CHAIN` unset - the chain is the default list
  `gemini-3.8-flash, gemini-3.7-flash, gemini-3.6-flash, gemini-3.5-flash`, with
  `LLM_MODEL` moved to the front if it is set to something outside that list. An existing
  `.env` that pins `LLM_MODEL` therefore keeps its model as the preferred one and gains
  fallbacks below it.
- `LLM_MODEL` stays defined and keeps its current default (`gemini-3.6-flash`) for logs
  and for anything that wants a single name; the chain head is the authority at call time.

New variables: `LLM_MODEL_CHAIN`, `LLM_MODEL_COOLDOWN_SECONDS` (3600),
`LLM_MODEL_AUTODISCOVER` (true, same truthiness helper as `NAMING_ENABLED`),
`LLM_MODEL_PATTERN`, `LLM_MODEL_REFRESH_SECONDS` (86400).

## Risks / Trade-offs

- **A configured chain entry can stop existing.** Task 1 verified that all four default
  entries exist today (see Context), but `gemini-2.5-flash` shows how it ends: a retired
  model answers `404` "no longer available to new users". `mark_unknown` on 404, the
  discovery cross-check, and the probe all treat a non-existent name as normal; the worst
  case is one rejected call per unknown name per process.
- **Automatic adoption reverses a deliberate `DESIGN.md` §3 rule** → adoption is gated on
  a schema-enforced probe, announced by push, recorded in the log, and revoked by
  `LLM_MODEL_AUTODISCOVER=false`. `DESIGN.md` §3 is rewritten in the same change rather
  than left contradicting the code.
- **A newer model can be worse for this task** (different tone, weaker German, stricter
  refusals) even though it passes the probe → the probe checks the contract, not quality.
  Mitigation is the switch-off variable plus the push notification that tells the user a
  model changed, so an unexplained drop in name or recipe quality has an obvious suspect.
- **A fallback model may cost more per call than the head** → the chain is ordered by the
  user's preference, and the whole path only runs when the preferred model is already
  refusing. The alternative today is a failed import.
- **In-memory cooldowns are lost on restart** → one extra rejected call after a restart.
  Accepted, see above.
- **Quota exhaustion and a per-minute burst limit are both 429** → the "second 429 in a
  row" rule is a heuristic. Worst case: a burst limit briefly parks a model for the
  cooldown while a fallback answers, which is a cheaper mistake than the reverse.
- **Concurrency**: several background imports can mark the same model exhausted at once →
  the lock plus idempotent map writes make that harmless; no read-modify-write beyond
  setting a deadline.

## Migration Plan

Additive and reversible in one variable, in this order:

1. Verify the live model list against the deployed key and fix the default chain
   (task 1.1). Nothing else in the plan depends on guessing which models exist.
2. Ship configuration and the chain module with `LLM_MODEL_AUTODISCOVER=false` in
   `.env.example` guidance for the first deployment if the operator wants to watch the
   fallback behaviour before enabling adoption. Default in code is `true`.
3. Deploy. No database migration, no new volume, no new dependency, no HTTP change.
4. Rollback: set `LLM_MODEL_CHAIN` to the single model in use today and
   `LLM_MODEL_AUTODISCOVER=false`. That reproduces pre-change behaviour exactly - one
   model, one transient retry - without reverting code.

## Open Questions

- Whether one hour is the right cooldown for a Gemini free-tier daily quota, which in
  practice resets at a fixed wall-clock time rather than after a fixed delay. Deferrable:
  it is one variable and it does not change the specs, the module boundary, or the tasks.
- Whether the adoption push should also fire when a model is marked unknown or exhausted
  for the first time. Left out for now to avoid a chatty channel; the log carries it.
