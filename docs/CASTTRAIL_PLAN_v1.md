# CastTrail: Plan v1

**Replaces:** HLD v0.12 and LLD v0.8, which are now background only. This is the single source of truth.
**Date:** 2026-11-07 · **Audience:** the developer and coding agents

## 1. Goal

A self-hosted app for 2-3 trusted users. It watches actors, films and series, finds UK convention guest appearances, and sends one ntfy alert per new relevant appearance. That is the whole core product.

**Optional collections:** users can also track simple per-user collections (e.g. "autographs I've collected"). **Bond is an add-on** on top of that: a poster challenge of 25 or 27 films with one unique in-person signer per poster, per collection. The core app must work fully with collections and Bond absent.

**Non-goals:** SaaS, ticketing, JSON API, email/SMTP, OIDC, CLI, scarcity ranking, collection sharing, generic scraper engine.

## 2. Decisions that change from the old plan

| Old | New | Why |
|---|---|---|
| 5 workspace packages with entry points | Single package. Providers live in `casttrail/providers/*` behind a `Protocol`. Entry points are deferred until a third-party provider exists. | Keeps the plugin boundary (architecture tests enforce it) without the packaging cost. |
| `/api/v1` JSON API | None. Jinja2 + HTMX only. | No consumer exists. Add read endpoints when one does. |
| FastAPI Users | `pwdlib` (argon2), own `users` and `sessions` tables, opaque session token (only its SHA-256 stored), cookie `HttpOnly`, `SameSite=Lax`, `__Host-Http-` prefix when Secure, one strict cookie reader. CSRF by `Sec-Fetch-Site`/`Origin` check on unsafe methods (as Menagerist), no form tokens. Login throttle per username and IP. | FastAPI Users may be in maintenance mode, and it has no CSRF or rate limit anyway. Verify, and fall back to it if the custom code grows. |
| Source feasibility checked at WP-03 | **WP-0 spike first** | It is the largest risk. |
| 95% coverage in 6 areas plus a ported checker | Menagerist's Ruff/mypy/pytest/prek/CI patterns, trimmed (§7). Overall coverage floor rising 80% to 90%, 95% only for auth, identity, matching and signer rules via `coverage report --include`. No custom script. | Same safety where bugs are costly, less ceremony. |
| Separate HLD, LLD, ADRs, handoffs | This doc, plus a short `docs/decisions.md` log. | Docs were drifting out of sync. |

## 3. Architecture

HTTP client: `httpx2` (added with the first provider, WP-2). One FastAPI app, one SQLite file in WAL mode, one worker, in-process APScheduler (pin 3.x). Write transactions use `BEGIN IMMEDIATE` to avoid `SQLITE_BUSY` on lock upgrades. Network calls never run inside a transaction.

```
src/casttrail/
  main.py config.py db.py
  auth/ web/ (routes, templates, static)
  providers/{base.py, tmdb.py, <source1>.py, <source2>.py}
  identity/ discovery/ matching/ collections/ notifications/ jobs/
  bond/            # film seed data, eligibility rules
migrations/  tests/{unit,integration,fixtures}
```

**Architecture tests (`tests/architecture`, archunitpython as in Menagerist; they run inside `pytest`):**
1. `providers` imports neither `db` nor services.
2. `identity`, `matching` and `collections` do not import `web`, FastAPI or Jinja.
3. Providers do not import each other.

**Provider contract.** It returns data and never touches the DB.

```python
class ListingResult(BaseModel):
    items: list[GuestObservation]
    complete: bool        # False on partial fetch, parse drift, or HTTP error
    etag: str | None

class ConventionProvider(Protocol):
    provider_id: str
    async def conventions(self) -> ConventionListing: ...   # same shape
    async def guests(self, source_key: str) -> ListingResult: ...
```

- `GuestObservation` fields: `source_key`, `name`, `source_url`, `identifiers: dict[str,str]` (e.g. `{"tmdb": "..", "imdb": ".."}`), and optional `attendance_days`, `autograph_available`, `autograph_price`, `status`.
- `None` means unknown. Explicit cancellation arrives only via `status=cancelled`.
- **Core removes or flags a guest as "no longer listed" only when `complete=True`.** Disappearance is never cancellation.

## 4. Key rules

**Identity**
1. An existing manual decision always wins.
2. A shared identifier already linked (tmdb or imdb) auto-links.
3. Otherwise, normalised name matches against **watched people only** produce a *Possible* suggestion. This is a small candidate set, so the match is cheap and usually right.
4. Anything else is unresolved.
5. Alerts fire only for **confirmed** links, which are auto-linked or admin-approved. *Possible* matches appear in the UI, labelled, with no alert.
6. The review UI supports bulk confirm of exact-name suggestions.
7. Rejections are stored per (external identity, candidate person) and are never retried.

**Alerts**
- Initial import of a provider (**baseline sync**) records everything and sends nothing.
- Only future-dated events alert.
- Production and series watches expand only to top-billed cast (configurable, default top 10).
- Event key is `appear:{canonical_appearance_id}:{person_id}`. A material change adds a new `rev` column value, not a hash. Per-user uniqueness is `(user_id, event_key)`.
- Delivery has a retry schedule of 1m, 5m, 30m, 2h, then failed. Delivery is at-least-once.
- ntfy: the server URL is set by the admin, and each user gets a random generated topic by default. Message text is sanitised.

**Bond signer uniqueness**
- `signatures(id, collection_id, item_id, person_id, in_person, ...)`.
- A composite FK `(collection_id, item_id)` references `collection_items`.
- A partial unique index enforces the rule:

```sql
CREATE UNIQUE INDEX uq_sig_signer ON signatures(collection_id, person_id)
  WHERE in_person = 1 AND enforce_unique = 1;
```

- `enforce_unique` is set from `collections.kind = 'bond'`. Generic collections use `0`.

**Multi-user:** every personal query filters by `user_id`. Both not-found and not-owned return 404. Shared data (people, conventions, appearances) is read-only for non-admins. Identity corrections are admin-only.

## 5. Schema, by slice

Alembic is set up at WP-1, and each slice adds only its own tables.

| WP | Tables |
|---|---|
| 1 | `users` |
| 2 | `people`, `productions`, `credits`, `external_identities` (unique provider+kind+external_id, nullable `entity_id`), `watches`, `franchises`, `franchise_productions` (manually curated, seeded with Bond) |
| 3 | `conventions`, `convention_sources` (convention ↔ provider+source_key, unique), `source_observations` (provider, source_key, content_hash, bounded `payload_json`, fetched_at; **prune to the last 3 per key**), `appearances` (nullable `person_id`, plus `external_identity_id`), `appearance_sources`, `job_runs` |
| 4 | `identity_decisions`, `matches` (non-null `match_key`, unique with `user_id`) |
| 5 | `convention_merges` (admin-confirmed event equivalence) only if auto-merge is not safe |
| 6 | `notification_events`, `user_notifications`, `notification_deliveries`, `ntfy_destinations` |
| 7 | `collections`, `collection_items`, `signatures`, `eligibility_overrides` |

## 6. Work packages

Each WP ends with a runnable app, a UI-visible outcome, a migration test (fresh DB, and upgrade from the previous slice), and green checks. **All tests use fixtures, never the live network.**

| WP | Deliverable | Done when |
|---|---|---|
| **0 Spike (about 2 d)** | Review ToS and robots for the candidate sites. Save fixtures. Check stable IDs, cancellation semantics, and whether names carry TMDB or IMDb links. Pick **two** sources, one of which should have richer attendance data. Write the outcome in `docs/decisions.md`. | At least one source is confirmed permitted and parseable. If none is, stop and replan before WP-1. |
| **1 Shell + auth** | `uv` project, FastAPI, Jinja2 + Pico + local assets, Alembic, Poe tasks (`check`), CI. Admin bootstrap from env, login and logout, CSRF, private dashboard, user admin. | Two users cannot see each other's pages. Health page shows version and DB revision. |
| **2 Watchlist** | TMDB search (people, films, series), credits, watch and unwatch, curated franchise watches. | Watch *Warehouse 13* and James Bond, restart, and they persist. |
| **3 Event browser** | Provider 1, discovery service, scheduled and manual refresh, source health, UK events and guests with provenance. | Refreshing twice makes no duplicates. A parse failure keeps old data and marks the source degraded. |
| **4 Relevant + identity** | Matching, "my relevant appearances" page, *Possible* tier, admin review with bulk confirm, link, unlink and reject. | A correction survives three re-syncs. Unresolved guests never alert. |
| **5 Second source** | Provider 2, event dedupe (strict: same country, overlapping dates, normalised name/venue match). Uncertain pairs stay separate, and the admin can merge them. Conflicts shown. | One real event from both fixtures shows once with two sources. |
| **6 ntfy** | Destination setup and test, outbox, baseline suppression, retries, delivery status page. | A new match alerts once. Re-ingest doesn't resend. Failed sends retry. |
| **7 Collections + Bond add-on** | Simple collections and signatures in core; Bond is a separate add-on module (`casttrail/bond/`, optional, switched on by a setting). Bond seed (25 films plus the 2 optional), partial unique signer index, eligibility overrides, and guests who can sign unsigned posters. | Two Bond collectors stay independent. The same signer can't fill two posters in one collection. |
| **8 Deploy** | Docker Compose, migrate-then-start, backup via SQLite backup API (`VACUUM INTO`) and restore test, two-user smoke test, README. | Fresh install works, and a restore from backup works. |

## 7. Tooling and CI (lean, borrowed from Menagerist)

**Rule:** copy Menagerist's config where it fits, and leave out everything for its frontend, Postgres, Docker, e2e, OpenAPI schema, Codecov and release flow.

**Copy (adapt names and paths)**

| From Menagerist | Use in CastTrail |
|---|---|
| `backend/pyproject.toml` `[tool.ruff]`, lint `select`, `ignore`, `isort`, `pydocstyle` (google), `mccabe`, format | Same rules, `known-first-party = ["casttrail"]`. Relax `D` for tests. Keep the `__future__.annotations` ban. |
| `[tool.mypy]` strict with `pydantic.mypy` plugin | Same; `files = ["src", "tests"]` |
| `[tool.pytest.ini_options]` with `asyncio_mode = "auto"`, `[tool.coverage]` with `branch = true`, `skip_empty`, `exclude_also` | Same. Keep the `integration` marker. Drop `docker` and `real_actor`. |
| Root `[tool.poe]` and the `sync`, `lint`, `format`, `typecheck`, `test` task pattern | Flat task list (below) |
| `.pre-commit-config.yaml` run by **prek**: `pre-commit-hooks` (whitespace, EOF, yaml/toml/json, merge conflict, `no-commit-to-branch main`, large files), conventional-pre-commit, gitleaks, validate-pyproject, check-github-workflows, actionlint | Keep these. Drop renovate, codecov, compose, schema and settings-docs hooks. |
| `ci.yml` pattern: pinned action SHAs (`checkout`, `setup-uv`), `UV_LOCKED=1`, `concurrency` group, `uv python install` then `uv run poe ...` | Use the same pinned SHAs. Drop the `changes` classifier job and the frontend jobs. |

**Poe tasks** (all run via `uv run poe <task>`)

| Task | Does |
|---|---|
| `sync` | `uv sync --group dev` |
| `format` / `format-check` | `ruff format` / `ruff format --check` |
| `lint` / `lint-fix` | `ruff check` / `ruff check --fix` |
| `typecheck` | `mypy` |
| `test` | `pytest --cov --cov-branch --cov-report=term-missing --cov-fail-under=<N>` |
| `check` | sequence of `format-check`, `lint`, `typecheck`, `test` |

**CI** is one workflow, `.github/workflows/ci.yml`, triggered on push to `main` and on pull requests. It has **two jobs**: `static` (format-check, lint, typecheck) and `test` (pytest with coverage, including the architecture tests and pytest-alembic checks). Both run on `ubuntu-latest` with `permissions: contents: read`. A Windows job is not required.

**Hooks (prek):** `pre-commit` runs the file-hygiene hooks plus `ruff format` and `ruff check --fix`. `commit-msg` checks Conventional Commits. `pre-push` blocks pushes to `main`. Tests and mypy run in CI, not in hooks, to keep commits fast.

**Coverage:** `--cov-fail-under` starts at 80 in WP-1 and rises to 90 by WP-8. Before WP-4, add a stricter `poe coverage-critical` task using `coverage report --include=... --fail-under=95` for `auth/`, `identity/`, `matching/` and the signer rules. No custom checker script.

**Required negative tests:** two-user isolation, 404 on others' IDs, CSRF failure, rejected candidate stays rejected, ambiguous guest gives no alert, incomplete listing deletes nothing, baseline sync sends nothing, duplicate signer rejected.

## 8. Estimate (human-reviewed, AI-assisted)

| WP | Days |
|---|---:|
| 0 | 2 |
| 1 | 3-4 |
| 2 | 3 |
| 3 | 4-6 |
| 4 | 4-5 |
| 5 | 3-5 |
| 6 | 2-3 |
| 7 | 3-4 |
| 8 | 2 |
| **Total** | **26-34** |

This is wide on purpose. Source quality (WP-0, 3, 5) is the main variance.

## 9. Deferred

Entry-point plugins, JSON API, SMTP, digests, Bond ranking and bipartite assignment, collection sharing, extra providers, OIDC, advanced identity scoring.

Revisit these only after WP-8, each as its own slice.

## 10. Open questions (answer inside WP-0 or WP-1)

1. Which two convention sites, and are they permitted?
2. Final auth choice (custom sessions or FastAPI Users)?
3. Is Python 3.14 compatible with every dependency's wheels? If not, use 3.13.

## 11. Chunk checklist

**Markers:** `[ ]` todo · `[x]` done · `🧪` the chunk must ship tests · `🗄️` the chunk adds a migration · `👁️ DEMO` a user-visible demo is needed to close the chunk · `⛔ GATE` stop and review before the next WP. A chunk is about half a day to a day, and each ends with `uv run poe check` green.

### WP-0 Spike
- [ ] 0.1 List candidate sites and check ToS, robots and rate limits
- [ ] 0.2 Save fixtures for 2 events and their guest lists per site
- [ ] 0.3 Record stable IDs, cancellation signals and external links per site
- [ ] 0.4 Pick 2 sources and write them in `docs/decisions.md`
- [ ] ⛔ GATE: at least one source is permitted and parseable, or replan

### WP-1 Shell + auth
- [x] 1.1a `uv` project, `src/` layout, Ruff + mypy + pytest config copied from Menagerist, Poe tasks (§7) 🧪
- [x] 1.1b `ci.yml` (two jobs: static, test) with pinned action SHAs; green on a trivial test
- [x] 1.1c prek config: file hygiene, ruff, conventional commits, gitleaks, `no-commit-to-branch`, actionlint
- [x] 1.2 FastAPI app factory, Jinja2 base, Pico, local assets, health page
- [x] 1.3 SQLAlchemy async + SQLite (WAL, `BEGIN IMMEDIATE`) and Alembic 🗄️
- [x] 1.4 `users` + `sessions` tables, argon2 hashing, login and logout, hashed server-side sessions, strict cookies 🗄️ 🧪
- [x] 1.5a CSRF by `Sec-Fetch-Site`/`Origin` on every unsafe method, login included (done with 1.4) 🧪
- [ ] 1.5b Login throttle per username and IP 🧪
- [ ] 1.6 Admin bootstrap from env, admin user management, private dashboard 👁️ DEMO
- [ ] 1.7 Extend the architecture tests as packages appear (providers, identity, matching)
- [ ] ⛔ GATE: two users are isolated, 95% coverage on `auth/`

### WP-2 Watchlist
- [ ] 2.1 TMDB provider with fixtures, `Protocol` in `providers/base.py` 🧪
- [ ] 2.2 `people`, `productions`, `credits`, `external_identities` 🗄️
- [ ] 2.3 Search UI (HTMX) for people, films and series
- [ ] 2.4 `watches` + watch and unwatch, per-user isolation 🗄️ 🧪
- [ ] 2.5 Franchises with the Bond seed and franchise watches 🗄️ 👁️ DEMO
- [ ] ⛔ GATE: watches survive restart, 404 on another user's watch

### WP-3 Event browser
- [ ] 3.1 Provider 1 parser against fixtures, `ListingResult.complete` logic 🧪
- [ ] 3.2 Convention, source, observation and appearance tables 🗄️
- [ ] 3.3 Discovery service: idempotent upsert, provenance, prune observations 🧪
- [ ] 3.4 `job_runs`, non-overlapping lock, scheduler, manual refresh
- [ ] 3.5 UK events and guests pages, unknowns shown as unknown 👁️ DEMO
- [ ] 3.6 Source health page; a parse failure keeps old data 🧪
- [ ] ⛔ GATE: double refresh gives no duplicates

### WP-4 Relevant appearances + identity
- [ ] 4.1 Identity resolver (manual > shared ID > watched-name *Possible* > unresolved) 🧪
- [ ] 4.2 `identity_decisions` + rejection persistence 🗄️ 🧪
- [ ] 4.3 Matching service and `matches` table with `match_key` 🗄️ 🧪
- [ ] 4.4 "My relevant appearances" page with match reasons 👁️ DEMO
- [ ] 4.5 Admin review UI: confirm, reject, link, unlink, bulk confirm exact names
- [ ] 4.6 Rematch is idempotent; the correction survives 3 re-syncs 🧪
- [ ] ⛔ GATE: 95% coverage on `identity/` and `matching/`

### WP-5 Second source
- [ ] 5.1 Provider 2 parser against fixtures 🧪
- [ ] 5.2 Strict event dedupe and optional `convention_merges` 🗄️ 🧪
- [ ] 5.3 Cross-source guest handling; conflicts and unknowns displayed
- [ ] 5.4 Admin merge for uncertain event pairs 👁️ DEMO
- [ ] ⛔ GATE: the same event from both fixtures shows once with two sources

### WP-6 ntfy alerts
- [ ] 6.1 Notification tables and event keys 🗄️ 🧪
- [ ] 6.2 ntfy adapter, admin-set server, random user topic, test button
- [ ] 6.3 Outbox worker with retries and crash recovery 🧪
- [ ] 6.4 Baseline sync suppression, future-only events, top-billed expansion 🧪
- [ ] 6.5 Delivery status page 👁️ DEMO
- [ ] ⛔ GATE: re-ingest sends nothing, a failed send retries, 95% coverage on notifications

### WP-7 Collections
- [ ] 7.1 Collection and item tables, ownership checks 🗄️ 🧪
- [ ] 7.2 Signatures with the partial unique index 🗄️ 🧪
- [ ] 7.3 Generic collections UI (*Warehouse 13*, *Top Secret!*) 👁️ DEMO
- [ ] 7.4 Bond seed (25 plus 2 optional), poster grid, eligibility overrides
- [ ] 7.5 Guests who can sign unsigned posters (no ranking)
- [ ] ⛔ GATE: duplicate signer rejected under concurrency, two Bond collectors are independent

### WP-8 Deploy
- [ ] 8.1 Dockerfile and Compose, migrate-then-start
- [ ] 8.2 Backup via `VACUUM INTO`, restore test 🧪
- [ ] 8.3 Two-user smoke test, security pass (cookies, `/docs` disabled)
- [ ] 8.4 README and `docs/decisions.md` cleanup 👁️ DEMO
- [ ] ⛔ GATE: fresh install and a restore both work
