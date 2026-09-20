# Tasks

Counts below are the measured baseline (`ruff 0.16.8`, `--isolated`, rule set
`E,F,I,B,BLE,C4,RUF`, line length 110): 41 findings across `src/` and `tests/`. Re-measure
after each group; the numbers are the acceptance criterion.

## 1. Tool and configuration

- [x] 1.1 Add `ruff==0.16.8` to `requirements-dev.txt` (pinned, like every other
  dependency in this repo) and confirm `pip install -r requirements-dev.txt` then
  `ruff --version` prints that version. `requirements.txt` stays untouched: the linter
  never enters the image.
- [x] 1.2 Create `pyproject.toml` with a `[tool.ruff]` section only - no
  `[build-system]`, no `[project]`: `target-version = "py312"`, `line-length = 110`,
  `src = ["src", "tests"]`, `lint.select = ["E", "F", "I", "B", "BLE", "C4", "RUF"]`,
  and `lint.flake8-bugbear.extend-immutable-calls = ["fastapi.File", "fastapi.Header",
  "fastapi.Depends"]`. Verified when `ruff check src tests` reports 40 findings (41
  minus the `B008` now exempted) and `pytest` is still green, proving the new file
  changed nothing about how the code is imported or installed.

## 2. Real findings, each in its own commit

- [x] 2.1 Fix `RUF006` at `src/app.py:79`: the resume-after-restart loop calls
  `asyncio.create_task(_run_import(...))` without holding the returned task, so the loop
  may garbage-collect an interrupted import before it finishes. Keep references in a
  module-level set and discard them in a done-callback, with a comment naming the
  failure mode. Verified by `tests/test_resume.py` still passing plus a new assertion
  that the resumed task ran to completion.
- [x] 2.2 Fix `B904` at `src/app.py:96`: the `HTTPException` raised for invalid JSON
  loses the original parse error. Re-raise with `from exc`, or `from None` if the
  original is deliberately hidden - decide by what the endpoint should report and say so
  in a comment. Verified by `ruff check` clean on that line and `tests/test_import_file.py`
  passing.
- [x] 2.3 Add `strict=` to the four `zip()` calls in `src/sources/document.py`
  (lines ~139, ~146, ~154, ~155). Each pairs PDF pages with their extracted text;
  decide per call whether a length mismatch is impossible (`strict=True`) or expected
  (`strict=False`) and note which in a comment. Verified by `tests/test_document.py`
  passing and the four findings gone.

## 3. The deliberate blind excepts

- [x] 3.1 Mark the 12 `BLE001` sites with `# noqa: BLE001 - <German reason>` in the form
  `src/sources/site.py` already uses: 9 in `src/app.py`, 2 in `src/image.py`, 1 in
  `src/naming.py`. The reason is one short clause from each function's docstring (never-
  raise stage, cleanup must not fail the import, and so on), not a repeat of the rule
  name. Verified when `ruff check` reports no `BLE001` and no `RUF100`, which together
  prove every marker is both present and actually suppressing something.
- [x] 3.2 Confirm the three existing markers in `src/sources/site.py` and the
  `# noqa: E402` in `tests/conftest.py` are now live rather than unused. Verified by
  `ruff check --select RUF100 src tests` reporting nothing, and by removing one marker
  temporarily to see the underlying finding appear.

## 4. Mechanical cleanup

- [x] 4.1 Run `ruff check --fix src tests` for the auto-fixable findings (4 `I001`
  import blocks in `tests/`, the `C408` in `tests/test_schema.py`) and read the diff
  before keeping it. Verified by `pytest` green and the diff containing nothing but
  import reordering and one dict literal.
- [x] 4.2 Rewrap the 17 `E501` lines over 110 characters, in a commit of its own so a
  rebase against the other changes in flight stays mechanical. Break at argument
  boundaries; where a log call would become unreadable, shorten the message instead of
  stacking continuations. Verified by `ruff check --select E501` clean and `pytest`
  green.
- [x] 4.3 Add `# noqa: RUF001 - live erfasster Mealie-Titel, EN DASH gehört zur Probe`
  to `tests/test_placeholder.py:20`. The EN DASH is recorded Mealie output from
  2026-08-24; changing the character would falsify the fixture. Verified by
  `ruff check tests/test_placeholder.py` clean with the fixture string unchanged.
- [x] 4.4 Run `ruff check src tests` and confirm it reports zero findings across the
  whole tree, then `pytest` and confirm the suite is green.

## 5. CI and documentation

- [x] 5.1 Add a `ruff check src tests` step to `.github/workflows/ci.yml`, before the
  `pytest` step and failing the job on findings. Verified by pushing a branch with one
  deliberate violation, seeing the job fail on that step, then removing it and seeing
  the job pass.
- [x] 5.2 Document the command in `README.md` under Tests: `ruff check src tests` next
  to `pytest`, with one line saying the config lives in `pyproject.toml` and that CI
  runs the same command. Verified by following the README from a clean checkout and
  getting a green run.
- [x] 5.3 Extend `DESIGN.md` §13 (Stil) with a short paragraph: which half of the style
  rules ruff now enforces, that `BLE001` is deliberately on, and the `# noqa: <CODE> -
  <Grund>` convention for the exceptions. Verified by reading §13 against
  `pyproject.toml`: no rule claimed there that the config does not set.
