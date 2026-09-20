# Proposal

## Why

The text stage talks to exactly one model: `config.LLM_MODEL` (`gemini-3.6-flash`), read
by `llm._post` for every extraction, image extraction and naming call. When that model's
quota is used up, the provider answers `429`, `_post` retries once after two seconds,
gets `429` again and raises `LlmOverloadedError` - the import dies and the human is asked
to share the link again, even though the same account can still answer on a different
model. The image-stage probe recorded in `src/config.py` shows this is not theoretical:
on this provider a per-model quota can be exactly zero while other models answer `200`.

A second, slower problem: the model name is pinned by hand (`DESIGN.md` §3, "fest
verdrahtet, kein wandernder Alias"). Nothing in the service reports that `gemini-3.7` and
`gemini-3.8` exist, so the service keeps using an older model until somebody happens to
read a changelog.

## What Changes

- **Model chain instead of a single model.** `LLM_MODEL_CHAIN` holds an ordered list,
  newest first (default `gemini-3.8-flash, gemini-3.7-flash, gemini-3.6-flash,
  gemini-3.5-flash`). The first entry is the model normally used; the rest are fallbacks.
  `LLM_MODEL` stays supported as the single-model form and as the chain's head.
- **A `429` switches models.** Today's behaviour is kept for the first `429` on a model
  (one retry on the same model after a short delay, which covers a per-minute burst
  limit). A second `429` marks that model as exhausted and the same call is re-sent to
  the next model in the chain. A second `502/503/504` also moves the call to the next
  model but leaves no mark: measured on this provider, that status names one model's
  load - `gemini-3.7-flash` answered `503` twice while two other models answered `200` in
  the same minute - and it passes in minutes, so a cooldown would be wrong. `500` keeps
  today's behaviour unchanged: one retry on the same model, then `LlmOverloadedError`.
- **A model the provider does not know is skipped, not fatal.** A `404` or a
  model-not-found `400` takes that name out of the chain for the process lifetime and the
  call moves on. Without this, a default chain that names a model this account does not
  have would fail every import.
- **Exhausted models are remembered process-wide** for a configurable cooldown
  (`LLM_MODEL_COOLDOWN_SECONDS`, default 3600) so the next import starts at the first
  model that is not known-exhausted instead of burning one failed call per import. The
  memory is in-process only; a restart clears it.
- **All models exhausted** raises `LlmOverloadedError` as today, with today's §7 wording.
  No new user-facing failure text, and no new failure mode.
- **Automatic adoption of newer models.** At startup, and then on a fixed interval, the
  service reads `GET {LLM_BASE_URL}/models`, keeps the entries matching a configured
  pattern (`LLM_MODEL_PATTERN`, default the `gemini-<major>.<minor>-flash` family),
  orders them by version and puts any model newer than the current chain head in front of
  the chain. A newly discovered model is probed once with a minimal schema-enforced call
  before it is used for real imports, and is discarded on failure - a model that cannot
  do `response_format: json_schema` must never break an import. Adoption is logged and
  pushed to Home Assistant once.
- **BREAKING (principle, not interface)**: this reverses `DESIGN.md` §3's "fest
  verdrahtet, kein wandernder Alias" for the text model. `LLM_MODEL_AUTODISCOVER=false`
  restores the pinned behaviour, and the chain then only ever contains configured names.
- The image stage (`src/image.py`, `IMAGE_MODEL`) is explicitly out of scope: its default
  provider Pollinations serves exactly one model and needs no key.
- No HTTP contract change, no new dependency, no new container.

## Capabilities

### New Capabilities
- `llm-model-fallback`: The ordered model chain for the text stage, the `429`-driven
  switch to the next model, the cooldown memory for exhausted models, the behaviour when
  the chain is used up, and the discovery, probing and adoption of newer models from the
  provider's model list.

### Modified Capabilities
<!-- The project has no specs under openspec/specs/ yet (`openspec list --specs` reports
     none), so there is no existing capability whose requirements change. The behaviour
     this change alters is currently described only in DESIGN.md §3 and §7. -->

## Impact

- `src/llm.py`: `_post` gains chain traversal and the status classification; the model name
  used for a call is no longer read straight from `config.LLM_MODEL`. `LlmOverloadedError`
  keeps its meaning ("nothing answered"), so `app.py` needs no change.
- New module for the chain state (current order, cooldowns, discovery/probing), so `llm.py`
  keeps holding only the transport and the schema retry.
- `src/config.py`: `LLM_MODEL_CHAIN`, `LLM_MODEL_COOLDOWN_SECONDS`, `LLM_MODEL_AUTODISCOVER`,
  `LLM_MODEL_PATTERN`, `LLM_MODEL_REFRESH_SECONDS`. `LLM_MODEL` keeps working unchanged.
- `src/app.py`: `lifespan` starts the discovery refresh task. No change to the failure
  paths or to `_describe_source_error`.
- `src/naming.py`: no change - it calls `llm._post` and inherits the chain.
- `src/ha_notify.py`: no code change, one new call site for the adoption notice.
- `DESIGN.md` §3 (config table and the pinned-model rule), §7 (the overload row gains the
  "all models exhausted" reading), plus `.env.example` and `README.md`.
- `tests/`: new tests for chain order, the `429` switch, the cooldown, chain exhaustion,
  discovery parsing/ordering, probe rejection, that `503` switches without a cooldown,
  and that `500` still does not switch.
- Interaction with the in-flight `add-transient-retry-queue` change: the fallback runs
  first and only raises `LlmOverloadedError` once every model is exhausted, so the queue
  change keeps its trigger and simply sees it less often. Neither change blocks the other.
