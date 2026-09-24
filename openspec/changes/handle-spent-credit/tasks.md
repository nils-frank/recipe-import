# Tasks

## 1. Shared recognition of a spent quota or credit

- [x] 1.1 Move the marker list that identifies a spent quota, credit or billing state into
  `src/llm.py` next to `_UNKNOWN_MODEL_MARKERS`, with a helper that answers "does this
  status and body mean spent?"; verify a unit test covers `402` alone, `403` with each
  marker word, `403` without one, and an unrelated `400`.
- [x] 1.2 Make `src/image.py` import that helper instead of carrying its own copy; verify
  the existing picture-stage tests for `429`, `402` and `403` still pass untouched.

## 2. The text stage walks the chain on a spent credit

- [x] 2.1 Add the branch to `llm._post` ahead of the transient-status block: mark the
  model exhausted, break to the next candidate, no second attempt on the same model;
  verify a test asserts exactly one request per model across a full chain of `402`
  answers.
- [x] 2.2 Make an exhausted-by-credit chain raise `LlmOverloadedError` with the model
  names it tried, like the quota case; verify a test asserts the class and that the
  message names the chain.
- [x] 2.3 Keep a pinned-model call (the discovery probe) failing without walking the
  chain; verify a test asserts the probe still raises rather than marking other models.
- [x] 2.4 Verify with a test that a `403` naming no credit, billing or quota reason still
  fails immediately as today, without touching the chain.

## 3. The import is parked, not discarded

- [x] 3.1 Verify with a test through the import flow that a spent-credit chain parks the
  entry with a due time instead of failing it, and that the notification is the queued
  wording rather than the "no recipe" one.
- [x] 3.2 Verify with a test that the parked entry completes on a later attempt once the
  model answers again, with no second sharing of the source.

## 4. Documentation

- [ ] 4.1 Update the status table for the text stage in `DESIGN.md` (§3/§7) with the
  spent-credit row and what it now causes; verify the table names every status the code
  classifies.
- [ ] 4.2 Correct the runbook in the operations repository
  (`~/Documents/github/homelab/setup-recipe-import.md`), which currently reads a `402` as
  the service being down, and commit it there; verify the runbook says the imports are
  parked and resume by themselves.

## 5. Gate

- [ ] 5.1 Run `ruff check .` and `pytest`; verify both are clean and no test needs network
  access.
- [ ] 5.2 Deploy to the host and verify against the live provider that a normal import
  still completes on the free tier; verify the log shows the text chain answering and the
  picture coming from the free image provider.
