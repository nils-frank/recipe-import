# Tasks

## 1. Configuration

- [x] 1.1 Add the queue settings to `src/config.py` (`QUEUE_POLL_SECONDS`,
      `QUEUE_BACKOFF_BASE_MINUTES`, `QUEUE_BACKOFF_FACTOR`,
      `QUEUE_OFFPEAK_THRESHOLD_MINUTES`, `QUEUE_OFFPEAK_WINDOW`, `QUEUE_MAX_ATTEMPTS`,
      `QUEUE_MAX_AGE_HOURS`, `QUEUE_PAYLOAD_DIR`, `QUEUE_PAYLOAD_MAX_MB`), all optional
      with the defaults from design.md; verify by importing `config` with none of them
      set and asserting the defaults, and with `QUEUE_OFFPEAK_WINDOW` malformed asserting
      a startup error that names the variable
- [x] 1.2 Document every new variable in `.env.example` with its default and effect;
      verify the file lists all nine names added in 1.1

## 2. Store schema and queue operations

- [x] 2.1 Extend `src/store.py` with the additive migration in `init()` (`attempts`,
      `due_at`, `payload` columns, guarded by `PRAGMA table_info`); verify with a test
      that runs `init()` twice over a database created by the old `SCHEMA` and asserts the
      columns exist once and existing rows keep their status
- [x] 2.2 Add `queue()`, `due(now)`, and an atomic `claim_due(url_hash)` that only moves
      `queued -> pending`; verify with tests for due/not-yet-due selection and for a
      second `claim_due` on the same row returning False
- [x] 2.3 Make `finish()` and `fail()` clear `due_at`/`attempts` on the terminal
      transition; verify a queued row that later finishes has no due time left

## 3. Retry scheduling

- [x] 3.1 Add a `schedule` module with a pure `next_due(attempts, now, jitter)`
      implementing backoff, jitter, and the off-peak switch-over; verify with table-driven
      tests for attempts 1..N delays, for the threshold crossing into the window, and for
      a `now` already inside the window scheduling into the current window
- [x] 3.2 Add `should_give_up(attempts, created_at, now)` covering both the attempt and
      the age bound; verify each bound triggers independently at its configured value

## 4. Transient failure classification

- [x] 4.1 Add `MealieUnavailableError(MealieError)` in `src/mealie_client.py`, raised at
      every site that converts a `requests.RequestException`, leaving status errors as
      `MealieError`; verify with tests that a connection error and a 500 response raise
      different classes
- [x] 4.2 Add `is_transient(exc)` to `src/app.py` covering `LlmOverloadedError`,
      `ThrottledError`, and `MealieUnavailableError`; verify with a test asserting True
      for those three and False for `NoRecipeFoundError`, `NoTranscriptError`,
      `SourceError`, `UnreadablePdfError`, `UnsupportedFileError`, plain `LlmError`, and
      plain `MealieError`
- [x] 4.3 Add the queueing notification texts from design.md to `_describe_source_error`'s
      neighbourhood as a separate reason-to-wording map; verify each transient class maps
      to its own message

## 5. Payload store for queued uploads

- [x] 5.1 Add a payload module that writes uploads under `QUEUE_PAYLOAD_DIR/<url_hash>/`,
      reads them back as `document.Upload` objects, deletes a hash's directory, reports
      total size, and sweeps orphan directories; verify with tests for a write/read
      round-trip preserving filename and content type, and for delete being idempotent
- [x] 5.2 Enforce `QUEUE_PAYLOAD_MAX_MB` before queueing a file import, failing the import
      permanently when over budget; verify a test that fills the budget and asserts the
      next queue attempt fails with a notification and leaves existing payloads untouched
- [x] 5.3 Run the orphan sweep from `lifespan` at startup; verify a test where a payload
      directory without a non-terminal row is removed and one with a `queued` row is kept

## 6. Queueing the three failure paths

- [x] 6.1 Route transient failures in `_run_import` to the queue (compute due time,
      increment attempts, store, notify once) and keep every permanent case on today's
      exact wording and `store.fail`; verify with tests that an overloaded LLM queues and
      that a `SourceError` still fails
- [x] 6.2 Do the same in `_process_file`, retaining the uploads via the payload store
      before queueing; verify a photo import hitting `LlmOverloadedError` leaves a
      `queued` row plus readable payload files
- [x] 6.3 Handle `MealieUnavailableError` in `_publish`'s create/rename path by queueing,
      while a `set_tags` failure stays cosmetic and never queues; verify with one test per
      branch
- [x] 6.4 Widen the duplicate-claim log wording in `_process_import`/`_process_file` to
      cover a `queued` row; verify a test that re-sharing a queued URL creates no second
      row and starts no second import

## 7. Scheduler

- [x] 7.1 Implement `_run_due_once(now)` in `src/app.py`: fetch due rows, give up where
      `should_give_up` says so (terminal failure, final notification, payload deleted),
      otherwise claim and re-run the import sequentially; verify with tests driving it
      directly with a fixed `now`, with no sleeping and no network
- [x] 7.2 Add the retry entry points that re-run a due URL import and a due file import
      from retained payloads without calling `store.start()`; verify `created_at` is
      unchanged after a retry and that a successful retry sends the normal success
      notification
- [x] 7.3 Start the polling task from `lifespan` and cancel it cleanly on shutdown; verify
      a test that startup creates the task and shutdown leaves no pending task
- [x] 7.4 Extend the startup resume so a `pending` file import with retained payloads
      resumes instead of failing, while one without payloads keeps today's "bitte die
      Datei noch einmal teilen"; verify one test per branch

## 8. Cross-cutting verification

- [x] 8.1 Add a rate-limit test asserting a due retry runs and does not increase
      `store.recent_count(3600)` even when the hourly limit is already exhausted
- [x] 8.2 Add an end-to-end style test with stubbed upstreams: a URL import fails
      transiently twice, then succeeds on the third due run, producing exactly one Mealie
      creation, one queueing notification, and one success notification
- [x] 8.3 Add a restart test: queue an import, re-run `lifespan` against the same
      database, and assert the row is retried at its due time and not duplicated
- [x] 8.4 Run the full suite and the project's lint step and verify both pass with no new
      warnings

## 9. Documentation

- [x] 9.1 Update `DESIGN.md`: drop "keine Queue" from the §5 non-goals with a dated note,
      describe the queue in §5/§6, replace the three transient rows in the §7 table with
      the queueing and give-up wording, and note the payload directory in §10; verify by
      reading the sections back for contradictions with the new behaviour
- [x] 9.2 Update `README.md` to state that a busy model or a throttled source is retried
      automatically and what the person sees; verify the endpoint table and the
      notification description still match the implementation
