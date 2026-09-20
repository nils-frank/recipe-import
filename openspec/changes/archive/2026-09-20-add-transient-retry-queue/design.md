# Design

## Context

See `proposal.md` - Why. The constraints that shape the approach:

- Processing today runs on FastAPI `BackgroundTasks` started from `src/app.py`, and
  `DESIGN.md` §5 names "keine Queue" as a Phase 1 non-goal. This change reverses that one
  non-goal and nothing else: no broker, no worker process, no second container.
- State lives in one SQLite table `imports`, keyed by `url_hash`, with statuses
  `pending | done | failed` (`src/store.py`). Idempotency, restart resume, and the hourly
  rate limit all read that table. The queue has to live inside that model rather than
  beside it, or there would be two answers to "is this source already being imported".
- `src/app.py` deliberately classifies extraction failures by exception **class name**
  (`_describe_source_error`) instead of importing the classes, for reasons its module
  docstring records. Any new classification has to fit that style or change it knowingly.
- Mealie failures are all one class today: `MealieError` is raised both for a
  `requests.RequestException` (unreachable) and for a non-2xx status (rejected). The spec
  needs those two apart.
- Uploaded bytes are never persisted - only their content hash - which is why
  `lifespan` marks every `pending` file import as failed on restart.
- Dependencies are kept small (`requirements.txt`, `DESIGN.md` §2); the container runs
  with `mem_limit: 512m` and a single bind mount `./data:/data` that already holds the
  SQLite file.

## Goals / Non-Goals

**Goals:**

- One durable place that answers "what is queued, when is it due, how often has it been
  tried" and survives a container restart.
- A scheduler simple enough to be tested without sleeping: time enters through one
  injectable clock, and due-time computation is a pure function.
- Retry work that is indistinguishable from first-attempt work once it starts, so the
  extraction and publish paths keep exactly one implementation.

**Non-Goals:**

- No learned or measured model of provider load. The off-peak window is configuration.
- No parallelism. Retries run one at a time; a capacity crunch is not a reason to send
  more concurrent requests.
- No queue for the permanent failure cases, and no user-visible queue inspection
  endpoint. The existing notifications stay the only interface.
- No change to the HTTP contract: both endpoints still answer `202` immediately.

## Decisions

### Queue state extends the `imports` table, no second table

Add to `imports`: `attempts INTEGER NOT NULL DEFAULT 0`, `due_at TEXT`, `payload TEXT`
(JSON metadata for retained uploads, `NULL` for URL imports), and a new status value
`queued`. `pending` keeps its meaning: an import being worked on right now.

Alternative considered: a separate `retry_queue` table joined on `url_hash`. Rejected
because every existing invariant - `start()`'s atomic claim, `_notify_if_done`, the rate
limit count - would then have to consult two tables to know a source's real state, and the
race-condition repair recorded in `store.start()` depends on a single atomic statement
over a single row.

Migration is additive: `ALTER TABLE imports ADD COLUMN ...` guarded by a read of
`PRAGMA table_info`, run from `store.init()`. Existing rows get `attempts = 0`,
`due_at = NULL`, `payload = NULL` and keep their status. No rollback step is needed - an
older build ignores the extra columns, and a `queued` row simply looks unknown to it,
which is why give-up also has an age bound.

### Two new store operations, not a new module for state

`store.queue(url_hash, due_at, attempts, payload)` moves a row to `queued`, and
`store.due(now)` returns `queued` rows whose `due_at <= now`. Claiming a due row for work
reuses the atomic-update pattern already proven in `start()`: a single
`UPDATE ... SET status = 'pending' WHERE url_hash = ? AND status = 'queued'` and a
`rowcount` check, so the scheduler and a concurrent user-triggered import cannot both own
the same row.

### One scheduler task, polling a due time

`lifespan` starts a single `asyncio` task that wakes on a fixed short interval
(`QUEUE_POLL_SECONDS`, default 30), asks `store.due(now)` for work, and runs each due item
to completion sequentially before sleeping again.

Alternatives considered:

- One `asyncio.sleep(delay)` task per queued item. Rejected: the schedule then lives in
  process memory, a restart loses it, and the SQLite row would only be a backup copy of
  something that already exists twice.
- APScheduler or a cron-style dependency. Rejected: a new dependency for one loop, in a
  project that keeps its dependency list short on purpose.

A 30-second poll is coarse relative to delays measured in minutes, costs one indexed
SQLite query per tick, and makes "due during downtime" fall out for free: after a restart
the first tick already finds it.

### Transient classification is an explicit predicate, not a string list at the call site

Add `is_transient(exc) -> bool` next to the existing `_describe_source_error` in
`src/app.py`, using the same class-name dispatch for `LlmOverloadedError` and
`ThrottledError`. For Mealie, introduce `MealieUnavailableError(MealieError)` in
`src/mealie_client.py`, raised at each site that currently converts a
`requests.RequestException` into a `MealieError`; a non-2xx status keeps raising plain
`MealieError`. That is the only way to honour the spec's split between "Mealie was
unreachable" and "Mealie said no" without parsing error text.

The three failure paths (`_run_import`, `_process_file`, and the Mealie half of
`_publish`) each get the same two-branch shape: transient goes to the queue, everything
else keeps today's exact behaviour and wording.

`_publish` needs one extra care point: a `MealieUnavailableError` from `set_tags` must
**not** queue anything, because the recipe already exists and the row is already `done` -
that path is already treated as cosmetic and stays that way.

### Backoff is a pure function; the off-peak window is a time-of-day range

`schedule.next_due(attempts, now) -> datetime` in a new small module, with no I/O:

- delay = `QUEUE_BACKOFF_BASE_MINUTES * QUEUE_BACKOFF_FACTOR ** (attempts - 1)`, default
  base 2 and factor 3, so 2, 6, 18, 54 minutes;
- jitter of ±25% applied to that delay;
- if the jittered delay exceeds `QUEUE_OFFPEAK_THRESHOLD_MINUTES` (default 60), the result
  is instead a random point inside the next occurrence of the off-peak window
  `QUEUE_OFFPEAK_WINDOW` (default `02:00-06:00`, container local time), where "next"
  means the current window if one is open and `now` plus a minimum spacing still fits.

Random placement inside the window rather than the window's start is deliberate: several
items parked on the same evening would otherwise all fire at 02:00 and recreate the
capacity problem locally.

Times are stored as UTC ISO-8601 strings, matching `created_at`/`updated_at`, and the
window is interpreted in local time so that "off-peak" means what the operator meant.

### Retained uploads live beside the database, referenced by row

Payload files go to `QUEUE_PAYLOAD_DIR` (default `<dirname(DB_PATH)>/queue`), one
directory per `url_hash`, one file per upload, with filename and content type carried in
the row's `payload` JSON. The compose file already bind-mounts `./data:/data`, so nothing
new has to be mounted; the deploy change is documentation of the extra space, not a new
volume.

`QUEUE_PAYLOAD_MAX_MB` (default 100) bounds the total. The check runs before the entry is
queued; over budget means the import fails permanently with a notification, never eviction
of another entry's content - evicting would strand a queue entry that can no longer run.

Deletion happens in exactly one place: the transition out of `queued`/`pending` into
`done` or `failed`. A startup sweep removes payload directories with no matching
non-terminal row, so a crash between "row updated" and "files deleted" cannot leak.

### Retries reuse the existing import path

A due item re-enters `_run_import` (URL) or a file variant that reads the retained bytes,
both after the row has been claimed as `pending`. Neither path calls `store.start()`,
which is what keeps `created_at` untouched - and `created_at` is what
`store.recent_count()` counts, so the rate-limit requirement needs no code of its own.

Sharing a queued URL again is already handled: `store.start()` only allows
`failed -> pending`, so a `queued` row rejects the duplicate claim. The log line there
needs its wording widened from "läuft bereits" to cover "is queued".

### Notification wording

New rows for `DESIGN.md` §7, German, in the register the table already uses:

| Case | Title | Message |
|---|---|---|
| Queued after transient failure | `Import später` | `Das Sprachmodell ist gerade ausgelastet. Ich versuche es automatisch später noch einmal.` |
| Queued, YouTube throttled | `Import später` | `YouTube drosselt gerade die Untertitel. Ich versuche es automatisch später noch einmal.` |
| Queued, Mealie unreachable | `Import später` | `Mealie ist gerade nicht erreichbar. Ich versuche es automatisch später noch einmal.` |
| Retries exhausted | `Import fehlgeschlagen` | `Auch nach mehreren Versuchen hat es nicht geklappt. Bitte noch einmal teilen.` |

The three existing "Bitte später erneut teilen" rows for the transient cases are removed
from the table, since those failures no longer end the import.

### Testability

`schedule.next_due` takes `now` and a jitter source as arguments; `store.due` takes `now`.
The scheduler loop's body is a separate coroutine (`_run_due_once`) that tests call
directly, so no test ever sleeps or starts the loop. This matches `DESIGN.md` §12's rule
that the suite runs without network access and without wall-clock dependence.

## Risks / Trade-offs

- **Retained uploads are user content on disk, with a longer lifetime than before** →
  Bounded by `QUEUE_PAYLOAD_MAX_MB` and by the age-based give-up, deleted on every
  terminal transition, swept at startup, and stored under the existing `/data` mount that
  is already treated as service state.
- **A queued import can succeed hours later, when the person has forgotten about it** →
  The queueing notification says so up front, and the success notification carries the
  recipe link as usual. The age bound keeps "later" from meaning "next week".
- **Off-peak window and DST / container timezone** → The window is local time and
  recomputed per scheduling decision, so a DST shift moves the window rather than
  corrupting a stored time; stored due times are absolute UTC and unaffected.
- **A provider outage longer than the age bound produces a burst of give-up
  notifications** → Accepted for a single-household service: the notifications are the
  only channel, and silence would be worse.
- **Sequential retries mean a long backlog drains slowly** → Intended. The backlog only
  grows when an upstream is unavailable, and draining faster is exactly what the upstream
  asked us not to do.
- **SQLite writes from a background task alongside request handlers** → Unchanged in kind
  from today: WAL is already on, writes are short, and the scheduler is a single task.
- **An older build after a rollback sees `queued` rows it does not understand** → They
  stay parked rather than running twice; the row is still visible as non-`done`, and a
  re-share moves it forward once the newer build is back. Called out in the migration plan
  rather than engineered around.

## Migration Plan

1. Deploy is a normal image update. `store.init()` performs the additive column migration
   on first start; no manual step, no downtime beyond the restart.
2. The startup payload sweep runs on the same first start and is a no-op on a fresh
   deployment.
3. Rollback is the previous image. No schema is removed, and the extra columns are
   ignored by the old build. Any row left in `queued` will not be retried by the old build
   - it can be reset by re-sharing the source, which the old build treats as a fresh
   import.
4. `.env.example` gains the new variables with the defaults above; all of them are
   optional, so an existing `.env` keeps working unchanged.

## Open Questions

- Whether the off-peak default of `02:00-06:00` is the right guess for the Gemini endpoint
  in use. It is configuration, so it can be tuned after observing real queue drain times
  without touching the specs or the task breakdown.
