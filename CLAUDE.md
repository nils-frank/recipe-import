# Notes for coding agents

## Where operations live

This repository holds the service only. The deployment it runs in - host, `.env` on that
host, compose file, deploy and rollback steps, and the notes that record what was measured
against the live system - lives in a separate repository: `~/Documents/github/homelab`,
starting at its `OPERATIONS.md` and the `recipe-import-*.md` notes.

Work from this repository and add the homelab one as a second working directory when a
task needs the deployment (`/add-dir ~/Documents/github/homelab`, or `claude --add-dir`).
The OpenSpec root is resolved from the current directory, so starting a session in the
homelab repository would put this project's planning artifacts in the wrong place.

Deliberately not written down here: the host address and any credential. Both were removed
from this repository's history once already. They belong in the homelab repository and on
the host, never in a file that is pushed to a public remote.

Changes to the deployment (a new environment variable, a changed compose file) are a
commit in the homelab repository, not here. An OpenSpec change that adds configuration
should carry an explicit task for that side.

## Conventions

The project's conventions - flat imports from `src/`, German comments against English
documentation, tests without network access, `ruff check .`, one change per branch with
several commits as the work proceeds - are written in `openspec/config.yaml` and surfaced
by `openspec instructions apply|archive`.
