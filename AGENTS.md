# AGENTS.md

CastTrail is a small self-hosted app for 2-3 trusted users. It tracks people, films and series, finds UK convention guest appearances, and sends ntfy alerts. Simple per-user collections exist too, and the Bond poster challenge is an optional add-on.

The plan is `docs/CASTTRAIL_PLAN_v1.md`. Read it before changing code. `docs/CASTTRAIL_HLD_v0.12.md` and `docs/CASTTRAIL_LLD_v0.8.md` are superseded background only.

## Commands

All via `uv run poe <task>`:

| Task | Purpose |
|---|---|
| `sync` | Install the project and dev tools |
| `format` / `format-check` | Ruff format / verify |
| `lint` / `lint-fix` | Ruff check / with safe fixes |
| `typecheck` | `mypy --strict` |
| `test` | pytest with branch coverage, including `tests/architecture` (archunitpython layering rules) and the pytest-alembic migration checks |
| `coverage-critical` | 95% coverage floor for `auth/`, `identity/` and `matching/` (run after `test`) |
| `check` | Everything CI runs. Run it before presenting work. |
| `hooks` | Install the prek git hooks |
| `serve` / `migrate` | Run the app locally / apply migrations by hand |
| `docker-up` / `docker-build` / `docker-down` / `docker-logs` | Build and run the Docker image (how it is meant to run); see `docs/operations.md` |
| `generate-icons` | Render the app icons from `logo.svg` (run when the logo changes) |

## Rules

- Python 3.14, `src/` layout, complete type annotations, Google-style docstrings.
- `from __future__ import annotations` is banned.
- HTTP client is `httpx2` (`import httpx2`), as in Menagerist. Do not use `requests` or `httpx`.
- No live network access in tests. Use saved fixtures.
- Control time in tests with the `time-machine` library. No helper clock classes, and do not add a `now` parameter just to make time testable.
- Never await an outside service (HTTP, a subprocess, a long sleep) while a session has a transaction open. Every transaction takes SQLite's single write lock when it begins, so one slow call locks everyone out ("database is locked"). Read what you need, commit or close, do the outside work, then begin a new transaction to write.
- Startup must never crash the app: a problem becomes a plain 503 page and a degraded `/health`, with detail only in the log (see `docs/operations.md`).
- Providers return data and never touch the database. Core owns persistence, identity, matching and notifications.
- Imported data is untrusted. A missing listing is never a cancellation. Manual identity decisions are never overwritten by a sync.
- Every personal query filters by user. Return 404 for missing or not-owned resources.
- Keep it simple. No speculative abstractions, buses or extra layers. Implement only the requested work package chunk.
- Each schema change is an Alembic migration with a fresh-database and an upgrade test.

## Git

- Never commit to `main`. One branch and one pull request per plan chunk (e.g. `feat/1-2-app-shell`, `fix/...`, `chore/...`).
- The PR title is a Conventional Commit. PRs are squash-merged and the branch is deleted on merge.
- A PR merges only when `CI passed` and `Hooks: prek (autofix)` pass and the branch is up to date with `main`.
- The autofix.ci app may push a `style: apply hook fixes` commit to a PR branch. Pull before pushing again. It never edits `.github/workflows`, so format those by hand.
- Commit messages follow Conventional Commits (`feat`, `fix`, `docs`, `ci`, `test`, `build`, `chore`, `refactor`).
- Do not commit or push unless asked.
- Do not add tool or assistant attribution to commits, pull requests, code or docs.
