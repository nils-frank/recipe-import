# Design

## Context

See proposal.md for motivation. The facts that shape the configuration, all measured
against the working tree with `ruff 0.16.8` and `--isolated` (no config file exists
anywhere in the repo, only a stale `.ruff_cache`):

| Rule set | Findings |
|---|---|
| ruff defaults | 23 |
| `E501` at 100 / 110 / 120 | 71 / 17 / 5 |
| chosen set `E,F,I,B,BLE,C4,RUF` at 110 | 41 |
| adding `UP,TRY,PLW,SIM` on top | +59, of which `TRY003` alone is 44 |
| `ruff format --diff` | all 34 files |

Constraints from the codebase itself:

- Never-raise stages are a documented design decision, not an oversight: `ha_notify`,
  `naming`, the new image stage and several `app.py` handlers catch `Exception` on
  purpose, and their docstrings say so. That is 12 `BLE001` sites.
- `TRY003` ("avoid long messages outside the exception class") fights the product
  directly: the German exception messages *are* the user-facing feedback that
  `DESIGN.md` §7 specifies word for word. 44 findings, all of which would be wrong.
- `B008` fires on `files: list[UploadFile] = File(...)` in `app.py`, which is how
  FastAPI is meant to be written.
- The repo already uses the `# noqa: <CODE> - <German reason>` form in four places.
- Python target is 3.12 (Dockerfile, CI).

## Goals / Non-Goals

**Goals:**

- One tool, one config block, one command that is identical locally and in CI.
- A configuration that encodes the project's existing conventions rather than importing
  a foreign style, so the backlog is finite and the rules stay credible.
- Keep `BLE001` as a live check rather than a global ignore.

**Non-Goals:**

- No formatter, no import-rewriting beyond `I001` ordering, no type checker. Whether to
  add `mypy` later is a separate question with a much larger backlog.
- No pre-commit framework. One `ruff check` in CI plus the documented local command is
  the direct path; a hook config is machinery to maintain for the same outcome.
- No repo-wide refactor in the name of a rule. Where a rule is wrong for this project it
  gets configured off or marked, with the reason written down.
- `ruff` does not enter `requirements.txt` and is not installed in the image.

## Decisions

### Config lives in a new `pyproject.toml`, tool section only

The repository has no `pyproject.toml` today because nothing is packaged: modules sit
flat in `src/` and the container copies them to `/app`. The file added here carries a
`[tool.ruff]` section and nothing else - no `[build-system]`, no `[project]` - so it
cannot be mistaken for a packaging change and `pip install -r requirements.txt` keeps
behaving exactly as it does now.

Alternative considered: `ruff.toml`. Rejected only because `pyproject.toml` is the file
a contributor looks in first, and a future tool can join it.

### Rule set `E, F, I, B, BLE, C4, RUF`, line length 110

Named explicitly rather than inherited from ruff's defaults: the default set moves
between ruff releases, and a pinned version plus an explicit list means a version bump
never silently adds findings to a green build.

- `E`, `F`: the baseline. Selecting all of `E` (not just ruff's default `E4,E7,E9`)
  turns on `E402`, which makes the existing `# noqa: E402` in `conftest.py` meaningful -
  that file genuinely imports after a `sys.path` change and says why.
- `I`: import order. 4 findings, auto-fixable.
- `B`, `BLE`, `C4`, `RUF`: the bug-shaped rules. This is where the payoff is - `RUF006`
  on the resume path and `B904` in the upload handler are both real.
- `UP`, `SIM`, `PLW`, `TRY` are left out. `TRY003` is the disqualifying one (44 findings
  against the deliberate German error texts); the rest are style opinions that would add
  churn without finding a defect. They can be added later, one family at a time.
- 110 characters: the code is written at roughly 100 with a tail of log-call and
  docstring lines that read worse when broken. 17 lines to rewrap, against 71 at 100.

### `BLE001` stays enabled and the 12 deliberate sites get markers

A global `ignore = ["BLE001"]` would be one line instead of twelve markers, but it also
deletes the rule's only real job: catching the *next* blind except, the one nobody
thought about. The repository already chose the other convention in `site.py`, and every
one of the 12 sites has a docstring explaining why it is broad, so the marker's reason
text is already written.

Each marker carries its reason in the existing form:

```python
except Exception as exc:  # noqa: BLE001 - Bildstufe darf den Import nie scheitern lassen
```

### `B008` is configured off for FastAPI's parameter defaults

Via `lint.flake8-bugbear.extend-immutable-calls` naming `fastapi.File`, `fastapi.Header`
and `fastapi.Depends`, rather than a `# noqa` on the one endpoint. The rule is right in
general and wrong for a framework whose dependency injection is built on exactly this
pattern, so the exemption belongs in the config where it is stated once and applies to
every future endpoint.

### Tests are linted too, with one relaxation

`tests/` gets the same rules. The one exception is the ambiguous-character rule
(`RUF001`) in `tests/test_placeholder.py`, which fires on the EN DASH in
`"Kartoffel – Wikipedia"`. That string is live-captured Mealie output recorded on
2026-08-24; "fixing" the character would falsify the fixture. It gets a `# noqa: RUF001`
with that reason.

### CI fails on findings; the same command runs locally

A `ruff check` step in the existing workflow, before `pytest` so a style failure reports
in a second instead of after the suite. A report-only step would be ignored within a
week. `README.md` gains the local command next to `pytest`, and `DESIGN.md` §13 gains a
pointer from the prose style rules to the tool that now enforces the mechanical half of
them.

### Pin the ruff version

`ruff==0.16.8` in `requirements-dev.txt`, pinned like every other dependency in this
repo (`requirements.txt` states the convention explicitly). An unpinned linter turns an
upstream release into a red build on an unrelated PR.

## Risks / Trade-offs

- [The 41-finding cleanup collides with the two changes in flight that touch the same
  files] → Land this change after them, and do the line-length pass in its own commit so
  a rebase conflict is mechanical.
- [`RUF006` is a behavior fix smuggled in with a tooling change] → It gets its own task
  and its own commit, with the reasoning recorded where the task list can point at it,
  rather than being folded into a bulk "fix lint" commit.
- [A pinned linter goes stale] → Acceptable: bumping it is a deliberate one-line change
  that shows its new findings in the same PR, which is the behavior the pin is for.
- [110 characters is a compromise nobody loves] → It is measured against this codebase
  rather than chosen from habit, and it is one line of config to revisit.
- [Enabling all of `E` may surface `E7xx` findings in files not yet examined] → The
  measured total already includes them; the count is 41 for the whole tree, not a
  sample.

## Migration Plan

Not a runtime change, so there is nothing to deploy or roll back in the service.

- Land config and dependency first, then the cleanup commits, then the CI step. The CI
  step goes last so the build never turns red on a commit that was still fixing findings.
- Rollback is deleting the CI step; the config and the markers are harmless on their own.
