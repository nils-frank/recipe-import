# Design

## Context

See `proposal.md` - Why, and `specs/llm-model-fallback/spec.md` for the requirements.

- Every text call goes through `llm._post`. Its status handling is a ladder: unknown model
  first, then `_RETRYABLE_STATUS = {429, 500, 502, 503, 504}` with one retry on the same
  model, and within that `_EXHAUSTING_STATUS = {429}` (mark exhausted, next model) and
  `_MODEL_BUSY_STATUS = {502, 503, 504}` (next model, no cooldown). Anything else raises
  `LlmError` on the spot.
- `app.is_transient` keys off the exception class name: `LlmOverloadedError`,
  `ThrottledError`, `MealieUnavailableError` are parked by the queue (A20), everything
  else is a final failure with a `DESIGN.md` §7 wording.
- Measured on 2026-09-24 against the deployed key: with prepay billing enabled and the
  credit spent, **every** model answers `402` with `"Your prepayment credits are
  depleted."` and `status: RESOURCE_EXHAUSTED` - text models and image models alike. The
  free tier is not used as a fallback while the project is in that state. Once prepay was
  removed, `gemini-3.5-flash` answered `200` again and the image models went back to
  answering `429` with a zero free-tier quota.
- The picture stage (A22) already sorts this answer correctly: its bucket "exhausted" is
  `429`, or `402`/`403` whose body names quota, credit, billing or `RESOURCE_EXHAUSTED`.
  On the day the text stage was discarding imports, the picture stage was quietly
  degrading to the free provider.

## Goals / Non-Goals

**Goals:**

- A spent credit costs the import a delay, not the import.
- One place that knows which words in a provider answer mean "spent", used by both stages.
- No new configuration, no new user-facing wording, no change to any other status.

**Non-Goals:**

- Watching, reporting or predicting the account balance. The provider says when it is out.
- Falling back to a different provider for text. The text chain is one provider's models;
  a second provider is a separate design with its own credentials and schema support.
- Changing what `429`, `500`, `502`, `503`, `504`, `404` or a malformed request do.

## Decisions

### Recognised by status plus wording, not by status alone

`402` counts on its own. `403` counts only when its body names credit, billing, quota or
`RESOURCE_EXHAUSTED`, because a plain `403` is just as likely to mean a revoked key, and
walking the chain for that would turn one useless call into four.

Alternative considered: treat any `4xx` that is not already classified as exhausting.
Rejected - a `400` for a malformed request would then be retried on every model in the
chain, four times the cost for the same certain failure.

### No second attempt on the same model

The existing `429` path sleeps two seconds and tries again, which is right for a
per-minute burst limit. A depleted credit is not a burst, so this case breaks out of the
attempt loop immediately. That also keeps the worst case cheap: one rejected call per
model, all of them refused instantly.

### The whole account, one model at a time

A spent credit is an account-wide fact, so in principle the first `402` could mark every
candidate at once. It does not: the code marks the candidate it actually asked and moves
on, which means one rejected call per model on the first import after the credit runs out,
and none at all on the following imports until the cooldown passes.

Inferring the account state from one model's answer would save three refused calls, all of
them free and instant, at the price of a rule that is false whenever a provider prices or
limits one model separately. The measured facts do not justify it: on this provider an
image model can be at zero while a text model answers `200`.

### The markers live in `llm.py`

`_UNKNOWN_MODEL_MARKERS` is already defined there and imported by `image.py`. The spent
markers follow it. One definition means a provider that changes its wording is fixed once,
and it makes the two stages visibly agree on what an answer means without either one
reaching into the other's state.

### The queue does the waiting

Nothing new is built for "retry when there is credit again". `LlmOverloadedError` is
already transient, so A20 parks the import, backs off exponentially, and moves long delays
into the off-peak window. An import that fails at 22:00 because the credit is gone is
retried on its own; if credit returns within `QUEUE_MAX_AGE_HOURS`, it completes without
anybody re-sharing the link.

## Risks / Trade-offs

- **An import now waits instead of failing fast.** Someone who shares a link during an
  outage gets "I will try again later" instead of an immediate answer → that is the
  behaviour A20 was built for, the wording already exists, and `QUEUE_MAX_AGE_HOURS`
  bounds how long "later" may mean.
- **A wrong marker match parks a call that can never succeed.** A `403` whose body happens
  to contain the word "quota" would be walked down the chain and then parked → the markers
  are specific, the chain walk costs four instant rejections, and the queue gives up after
  `QUEUE_MAX_ATTEMPTS`.
- **The provider changes its wording.** A future body that says neither "credit" nor
  "billing" falls back to today's behaviour, a failed import → the status `402` alone is
  already enough, so only the `403` variant depends on wording.

## Migration Plan

Code only; no configuration, no data, no container change. Deploy, and the next import
that meets a spent credit is parked instead of discarded. Rolling back is reverting the
commit: nothing persists that a previous version cannot read, because the queue entries
this produces are ordinary parked imports.
