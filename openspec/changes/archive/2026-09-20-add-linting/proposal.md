# Proposal

## Why

The repository has no linter. CI runs `pytest` and nothing else, there is no
`pyproject.toml`, no `setup.cfg`, no `.flake8` and no pre-commit hook. The code was
nevertheless written as if a linter existed: `src/sources/site.py` carries three
`# noqa: BLE001 - <reason>` markers and `tests/conftest.py` a `# noqa: E402`. Those
markers currently suppress nothing, and one of them (`E402`) would today be reported as
an unused directive.

Running ruff over the repository once already surfaces a real defect that the test suite
cannot see: `src/app.py:79` starts the resume-after-restart task with
`asyncio.create_task(...)` without keeping a reference (`RUF006`), so the event loop may
garbage-collect an interrupted import mid-flight. A linter is the cheapest way to keep
finding that class of bug, and it settles import order and line length so future diffs
stay about content.

## What Changes

- Add `ruff` as the project's linter, pinned in `requirements-dev.txt` alongside
  `pytest`, configured in a new `pyproject.toml` (the repository's first).
- Rule set `E, F, I, B, BLE, C4, RUF` at a line length of 110, which makes the existing
  `# noqa` markers meaningful and matches how the code is actually written.
- `BLE001` stays **on**. The service's never-raise stages keep their broad
  `except Exception`, each marked `# noqa: BLE001 - <reason>` exactly as `site.py`
  already does, so a *new* accidental blind except is still caught.
- `B008` is configured away for FastAPI's `File`/`Header` argument defaults, which are
  the framework's intended idiom, not a mistake.
- Work off the resulting backlog of 41 findings: 17 long lines, 12 blind-except markers,
  4 import blocks, 4 `zip()` calls without `strict=`, and 4 single findings including
  the `create_task` reference and one `raise ... from`.
- Add a `ruff check` step to the existing CI workflow that fails the build, and document
  the command in `README.md` next to `pytest`.
- No formatter. `ruff format` would rewrite all 34 files and bury the hand-wrapped German
  comment blocks; formatting stays a human decision.
- No change to the service's runtime behavior, its endpoints, its configuration or its
  dependencies at runtime. `ruff` is a development dependency only and is not installed
  into the container image.

## Capabilities

### New Capabilities
<!-- None. -->

### Modified Capabilities
<!-- None. This change is tooling: it adds a development dependency, a config file and a
     CI step. The service's observable behavior is unchanged, so `.openspec.yaml` sets
     `skip_specs: true` rather than inventing a requirement to satisfy validation. The
     two code fixes the linter forces (RUF006, B904) correct defects against behavior
     that DESIGN.md §5 already describes; they do not change what is specified. -->

## Impact

- New files: `pyproject.toml` (ruff config only, no build system, no packaging).
- Changed: `requirements-dev.txt` (one pinned line), `.github/workflows/ci.yml` (one
  step), `README.md` (how to run it), `DESIGN.md` §13 (the style section gains the tool
  that now enforces part of it).
- Touched for findings: `src/app.py`, `src/image.py`, `src/naming.py`,
  `src/sources/document.py`, `src/sources/youtube.py`, `tests/conftest.py` and five test
  modules. All mechanical except `RUF006` in `app.py`, which is a behavior fix.
- CI: one extra step, roughly a second of runtime. Contributors get a failing build for
  a style violation, which is the point, and the same command runs locally.
- Risk of conflict: the `add-ai-recipe-image` and `add-transient-retry-queue` changes
  touch some of the same files. This change should land after whichever of them is
  already in flight, or its line-length pass will collide with theirs.
