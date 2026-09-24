# Proposal

## Why

On 2026-09-24 every import of this service failed outright, and the reason was one status
code the text stage does not know:

```
402 "Your prepayment credits are depleted."   status: RESOURCE_EXHAUSTED
```

`llm._post` sorts answers into "try the next model" (429, 502, 503, 504), "this model does
not exist" (404 and some 400s) and "a real error of this call" (everything else). A `402`
lands in the last bucket, so it raises `LlmError`: the import is not moved to another
model, and it is not parked by the retry queue (A20) either, because only
`LlmOverloadedError`, `ThrottledError` and `MealieUnavailableError` count as transient.
The import is thrown away, and the user is told the source had no recipe on it - which is
false.

A spent credit is the same kind of fact as a spent quota: the provider is saying "not
now", not "never". The picture stage already treats it that way since A22 and degraded
cleanly on the same day, on the same account, in the same hour the text stage was
discarding imports.

## What Changes

- **A rejection naming a spent credit moves the call to the next model**, exactly as a
  `429` does today: the model is marked exhausted for the configured cooldown and the
  same, unchanged call goes to the next candidate. Recognised as `402`, or `403` whose
  body names credit, billing, quota or `RESOURCE_EXHAUSTED`.
- **No second attempt on the same model for this case.** The existing `429` path retries
  once after a short delay because a per-minute burst limit passes in seconds. A depleted
  credit does not, so the retry would only be a second rejection.
- **A chain that is spent fails as "try later", not as "no recipe".** With every candidate
  exhausted the call raises `LlmOverloadedError`, which the retry queue already treats as
  transient: the import is parked with a due time and retried, so it completes by itself
  once there is credit or the free tier returns. The user-facing wording is the one that
  exists today for that case.
- **One definition of the markers.** The strings that identify a spent quota or credit in
  an answer body live in `llm.py` and are imported by the picture stage, the way
  `_UNKNOWN_MODEL_MARKERS` already is, instead of each stage carrying its own list.
- Fixes a spec statement that A22 made false: `llm-model-fallback` still claims the image
  stage uses a single configured model. It has had its own chain since A22.
- No new configuration value, no new failure text, no new dependency.

## Capabilities

### New Capabilities

<!-- None. This extends how the existing text-stage chain classifies one provider answer. -->

### Modified Capabilities

- `llm-model-fallback`: the requirement covering a quota rejection is widened to cover a
  rejection naming a spent credit, with the difference that this case is not retried on
  the same model first. The requirement stating that the image stage is unaffected is
  corrected to match what A22 built: the image stage has its own chain and its own
  cooldown, and this capability governs the text stage only.

## Impact

- `src/llm.py`: one classification branch before the transient-status block, plus the
  shared marker list. `LlmError` and `LlmOverloadedError` keep their meanings.
- `src/image.py`: imports the shared markers instead of defining its own copy. No
  behaviour change - the picture stage already treats `402` as exhausted.
- `src/app.py`, `src/schedule.py`, `src/store.py`: unchanged. The parking behaviour comes
  for free, because the failure is already the class the queue looks for.
- `tests/`: the spent-credit walk down the chain, that it does not retry on the same
  model, that an exhausted chain raises the transient class, and that an import in that
  state is parked rather than failed.
- `DESIGN.md` §3/§7 (the status table for the text stage) and the runbook in the
  operations repository, which currently tells the reader a `402` means the service is
  down.
