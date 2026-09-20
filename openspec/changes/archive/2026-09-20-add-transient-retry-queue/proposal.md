# Proposal

## Why

When an upstream dependency says "try later" rather than "this cannot work", the import
is currently thrown away and the human is asked to share the link or the file again.
This happens on three observed paths: the LLM provider answering `429/500/502/503/504`
twice in a row (`LlmOverloadedError`, `src/llm.py`), YouTube throttling subtitle
downloads (`ThrottledError`, `src/sources/youtube.py:190`), and Mealie being briefly
unreachable (`MealieError` raised from a `requests.RequestException`). In every one of
those cases the input was fine and the same attempt would have succeeded minutes later.

The Gemini capacity case is the sharpest: the provider rejects the call because of its
own load, the service turns that into "Bitte später erneut teilen", and the user has to
remember to come back. A shared recipe should survive a busy model without human
bookkeeping.

## What Changes

- A durable retry queue replaces "fail immediately" for transient upstream failures.
  A failing import that is classified as transient is parked with a due time instead of
  going to `failed`, and a background scheduler picks it up when it comes due.
- Transient classification covers `LlmOverloadedError`, `ThrottledError`, and Mealie
  failures caused by a connection error rather than an HTTP status. Everything else
  (no recipe found, unreadable PDF, unsupported file, schema failure, Mealie 4xx)
  still fails immediately with today's wording.
- Retry timing is exponential backoff with jitter (minutes, not seconds). Once the
  computed delay crosses a configured threshold, the item is instead parked into the
  next configured off-peak window, on the assumption that provider capacity is easier
  to get outside peak hours. The window is configuration, not a learned model.
- A give-up policy ends the queue: after a configured number of attempts or a maximum
  age, the item goes to `failed` with a final push notification.
- Uploaded files become retryable. Their bytes are persisted next to the SQLite
  database while the item is queued, and deleted as soon as the import finishes, fails
  finally, or is abandoned. Today only the content hash is stored, which is why
  `src/app.py` marks every `pending` file import as failed on restart.
- The user gets a push notification when an import is queued ("wird automatisch später
  versucht"), and the normal success or final-failure notification later. No import is
  ever silently parked.
- The queue survives a restart: due times live in SQLite, and the scheduler resumes
  queued items at startup the same way `store.pending()` resumes interrupted ones.
- Queued retries do not consume the hourly rate limit a second time - the limit counts
  distinct accepted imports, not attempts.
- **BREAKING** (internal, no HTTP contract change): the `imports` table gains columns
  and a new `queued` status. Existing rows migrate in place; `pending` keeps its meaning
  of "being worked on right now".

## Capabilities

### New Capabilities
- `import-retry-queue`: Durable queueing and rescheduling of imports whose failure came
  from a temporarily unavailable upstream (LLM provider capacity, YouTube throttling,
  Mealie unreachable), including retry timing, the off-peak window, the give-up policy,
  the persistence of uploaded file bytes for queued file imports, and the user-facing
  notifications for queued, resumed, and abandoned imports.

### Modified Capabilities
<!-- The project has no specs under openspec/specs/ yet (`openspec list --specs`
     reports none), so there is no existing capability whose requirements change.
     The behaviour this change alters is currently described only in DESIGN.md. -->

## Impact

- `src/store.py`: schema migration (new `queued` status, attempt count, due time,
  payload reference), plus queries for due items.
- `src/app.py`: transient-vs-permanent classification in the two failure paths
  (`_run_import`, `_process_file`) and in `_publish`; a background scheduler started
  from `lifespan`; startup resume extended to queued items; the file-import branch in
  `lifespan` no longer fails uploads outright.
- `src/config.py`: new environment variables for backoff, off-peak window, attempt and
  age limits, and the payload directory and its size cap.
- `src/ha_notify.py`: no code change expected, new call sites only.
- New module for the queue payload store (uploaded bytes on disk).
- `DESIGN.md` §5, §6, §7, §10, §11: the "keine Queue" non-goal and the §7 wording table
  both change. `.env.example` and `README.md` need updating. `deploy/compose.yaml` needs
  no new volume - the retained uploads live under the existing `./data:/data` mount.
- `tests/`: new tests for classification, backoff, off-peak scheduling, give-up,
  payload lifecycle, and restart resume; `tests/test_ratelimit.py` gains a case that a
  retry does not re-charge the hourly limit.
