# CastTrail: Plan v1

**Replaces:** HLD v0.12 and LLD v0.8, which are now background only. This is the single source of truth.
**Date:** 2026-11-07 · **Updated:** 2026-11-09 · **Audience:** the developer and coding agents

## 1. Goal

A self-hosted app for 2-3 trusted users. It watches actors, films and series, finds UK convention guest appearances, and sends one ntfy alert per new relevant appearance. That is the whole core product.

**Optional collections:** users can also track simple per-user collections (e.g. "autographs I've collected"). **Bond is an add-on** on top of that: a poster challenge of 25 or 27 films with one unique in-person signer per poster, per collection. The core app must work fully with collections and Bond absent.

**Non-goals:** SaaS, ticketing, JSON API, OIDC, CLI, scarcity ranking, collection sharing, generic scraper engine.

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

HTTP client: `httpx2` (added with the first provider, WP-2). One FastAPI app, one SQLite file in WAL mode, one worker, in-process APScheduler (pin 3.x, jobs held in memory). Write transactions use `BEGIN IMMEDIATE` to avoid `SQLITE_BUSY` on lock upgrades. Network calls never run inside a transaction. It runs from a Docker image (`compose.yaml`), brings its own database up to date at startup after taking a copy, and starts in a "needs attention" mode instead of crashing when something is wrong (see `docs/operations.md`).

```
src/casttrail/
  main.py config.py db.py startup.py maintenance.py
  auth/ catalogue/ watching/ web/ (routes, templates, static)
  providers/{base.py, tmdb.py, rostercon.py, rostercon_client.py, comiconomicon.py}
  identity/ (decisions)      matching/ (resolver, service, queries, review)
  discovery/ (service, dedupe, merging, refresh, schedule, browse, health)
  planned: notifications/ collections/ bond/
migrations/ (0001 to 0013)   tests/{unit,integration,architecture,fixtures}
scripts/ (generate_icons.py, healthcheck.py)   Dockerfile compose.yaml
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

**As built.** Each source is a pure parser (HTML or JSON in, data out, no network and no database) plus, for Rostercon, a polite fetcher (at least 10 s between requests, cooldown after 429 or 503, capped body, only its own host). A parser returns the convention and a `ListingResult`; core's `DiscoveryService.ingest(provider, convention, listing)` stores it. Rostercon lists are complete only when the page says so; Comiconomicon's are never complete, because its guest lists come from automated matching. There is no `ConventionProvider` class: the two sources differ too much for one shape to help.

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
- Channels: ntfy is the first. Email (SMTP) and PWA Web Push are likely to follow as further channels (see section 9), so keep the outbox channel-agnostic. A delivery records its `channel`, a user's destinations are per channel, and nothing about the event key, `rev`, baseline suppression or the retry schedule is ntfy-specific. Settle the destination table's shape at 6.1 (one table holding a channel and its settings, not `ntfy_destinations`).

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
| 3 | `conventions`, `convention_sources` (convention ↔ provider+source_key, unique), `source_observations` (provider, source_key, content_hash, complete, fetched_at; no stored payload; **prune to the last 3 per key**), `appearances` (nullable `person_id`, plus `external_identity_id`), `appearance_sources`, `job_runs` |
| 4 | `identity_decisions` (migration 0010), `matches` (0011; non-null `match_key`, unique with `user_id`) |
| 5 | `convention_merges` (suggested and rejected pairs; 0012), and each source's own name, dates and venue on `convention_sources` (0013) |
| 6 | `notification_events`, `user_notifications`, `notification_deliveries`, `ntfy_destinations` |
| 7 | `collections`, `collection_items`, `signatures`, `eligibility_overrides` |

Built so far: migrations 0001 to 0013 (0009 `job_runs`, 0010 `identity_decisions`, 0011 `matches`, 0012 `convention_merges`, 0013 source views).

## 6. Work packages

Each WP ends with a runnable app, a UI-visible outcome, a migration test (fresh DB, and upgrade from the previous slice), and green checks. **All tests use fixtures, never the live network.**

| WP | Deliverable | Done when |
|---|---|---|
| **0 Spike (about 2 d)** | Review ToS and robots for the candidate sites. Save fixtures. Check stable IDs, cancellation semantics, and whether names carry TMDB or IMDb links. Pick **two** sources, one of which should have richer attendance data. Write the outcome in `docs/decisions.md`. | At least one source is confirmed permitted and parseable. If none is, stop and replan before WP-1. |
| **1 Shell + auth** | `uv` project, FastAPI, Jinja2 + Bootstrap 5.3 + local assets, Alembic, Poe tasks (`check`), CI. Admin bootstrap from env, login and logout, CSRF, private dashboard, user admin. | Two users cannot see each other's pages. Health page shows version and DB revision. |
| **2 Watchlist** | TMDB search (people, films, series), credits, watch and unwatch, curated franchise watches. | Watch *Warehouse 13* and James Bond, restart, and they persist. |
| **3 Event browser** | Provider 1, discovery service, scheduled and manual refresh, source health, UK events and guests with provenance. | Refreshing twice makes no duplicates. A parse failure keeps old data and marks the source degraded. |
| **4 Relevant + identity** | Matching, "my relevant appearances" page, *Possible* tier, admin review with bulk confirm, link, unlink and reject. | A correction survives three re-syncs. Unresolved guests never alert. |
| **5 Second source** | Provider 2, event dedupe (strict: same country, overlapping dates, normalised name/venue match). Uncertain pairs stay separate, and the admin can merge them. Conflicts shown. | One real event from both fixtures shows once with two sources. |
| **6 ntfy** | Destination setup and test, outbox, baseline suppression, retries, delivery status page. | A new match alerts once. Re-ingest doesn't resend. Failed sends retry. |
| **7 Collections + Bond add-on** | Simple collections and signatures in core; Bond is a separate add-on module (`casttrail/bond/`, optional, switched on by a setting). Bond seed (25 films plus the 2 optional), partial unique signer index, eligibility overrides, and guests who can sign unsigned posters. | Two Bond collectors stay independent. The same signer can't fill two posters in one collection. |
| **8 Deploy** | Docker Compose, migrate-then-start and backups (both built early, see 8.1 and 8.2), restore test, two-user smoke test, README. | Fresh install works, and a restore from backup works. |

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
| `check` | sequence of `format-check`, `lint`, `typecheck`, `test`, `coverage-critical` |
| `coverage-critical` | 95% floor for `auth/`, `identity/` and `matching/` (needs a prior `test`) |
| `serve` / `migrate` | Run the app locally (keep-alive off) / apply migrations by hand |
| `generate-icons` | Render the app icons from `logo.svg` (run when the logo changes) |
| `docker-up` / `docker-build` / `docker-down` / `docker-logs` / `docker-config` | Build the image (version from the `calver-scm` CLI) and run it; see `docs/operations.md` |

**CI** is one workflow, `.github/workflows/ci.yml`, triggered on push to `main` and on pull requests. It has **two jobs**: `static` (format-check, lint, typecheck) and `test` (pytest with coverage, including the architecture tests and pytest-alembic checks). Both run on `ubuntu-latest` with `permissions: contents: read`. A Windows job is not required.

**Hooks (prek):** `pre-commit` runs the file-hygiene hooks plus `ruff format` and `ruff check --fix`. `commit-msg` checks Conventional Commits. `pre-push` blocks pushes to `main`. Tests and mypy run in CI, not in hooks, to keep commits fast.

**Coverage:** `--cov-fail-under` starts at 80 in WP-1 and rises to 90 by WP-8. `poe coverage-critical` (done) holds `auth/`, `identity/` and `matching/` to 95%; the signer rules join it in WP-7. Actual coverage is about 99% overall. No custom checker script.

**Test practices (project owner's direction).** Time is controlled with the `time-machine` library, not helper clocks or an injected `now` (older code still has them; see the checklist). New logic gets mutation checks, run with a harness that restores the files in a `finally`. Tests never read a developer's `.env`. No live network in tests.

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

Entry-point plugins, JSON API, digests, Bond ranking and bipartite assignment, collection sharing, extra providers, OIDC, advanced identity scoring.

**Installable PWA (optional).** Web app manifest, the 1.6e icons, a service worker that caches only static assets, and an install prompt. Notes: it needs HTTPS, so it follows WP-8; the service worker must never cache pages or responses behind a login, so it stays a shell and offers no offline data; and a tap on an ntfy alert should open the installed app (the `SameSite=Lax` decision already allows that link to arrive signed in). Do 1.6e first so the icon set is ready. Web Push (below) builds on it.

**More notification channels (planned after WP-6).** Each is its own slice behind the channel-agnostic outbox.
- *Email (SMTP):* an admin-set server, a per-user address, a plain-text body built from the same sanitised message, and a test button. It is only a new adapter.
- *Web Push to an installed PWA:* needs the PWA first (manifest, service worker, HTTPS), VAPID keys held as admin-set secrets, one stored push subscription per browser, and removal of a subscription when the push service answers 404 or 410. iOS delivers push only to a PWA added to the home screen.

Revisit these only after WP-8, each as its own slice.

**Planned, from the build.**
- *Bootstrap 5.3 instead of Pico:* **done** (A5, issue #50). Vendored 5.3.8, not the 6.0 alpha.
- *Catch-up refresh at startup.* If the refresh is on and the last success is over a day old, run it after the app is up, as the startup backup does. Preferred over an APScheduler SQLAlchemy jobstore, whose pickled jobs cannot carry our live objects and which needs a table outside our migrations.
- *Restore from the admin page and an off-machine copy of backups.* Restore is a documented manual step for now.
- *Building and publishing the Docker image in CI.*
- *GitHub issue templates* (issue #51).

## 10. Open questions

Answered:
1. **Which two sites?** Rostercon (primary) and Comiconomicon. Neither grants permission to scrape, so both are used at personal scale as the owner's call, recorded in `docs/decisions.md`. Comiconomicon's search box (`/xhr/indexautocomplete.php`) is used to turn names into its ids, also the owner's call.
2. **Auth?** Custom sessions (not FastAPI Users).
3. **Python 3.14?** Works, including in the Docker image.

Still open:
4. (For WP-6) Which channels besides ntfy, and in what order: SMTP, Web Push?
5. (For 5.5d) How a watched film or series finds its Comiconomicon film page beyond an exact title and year match, and whether its `/universe` pages are worth using for franchise watches.
6. Whether the daily refresh should be on by default (it is off, so nothing contacts a source until the owner chooses).

## 11. Chunk checklist

**Markers:** `[ ]` todo · `[x]` done · `🧪` the chunk must ship tests · `🗄️` the chunk adds a migration · `👁️ DEMO` a user-visible demo is needed to close the chunk · `⛔ GATE` stop and review before the next WP. A chunk is about half a day to a day, and each ends with `uv run poe check` green.

### WP-0 Spike
- [x] 0.1 List candidate sites and check ToS, robots and rate limits
- [x] 0.2 Save fixtures for 2 events and their guest lists per site
- [x] 0.3 Record stable IDs, cancellation signals and external links per site
- [x] 0.4 Pick 2 sources and write them in `docs/decisions.md`
- [x] ⛔ GATE: at least one source is permitted and parseable, or replan (passed with caveats, see `docs/decisions.md`)

### WP-1 Shell + auth
- [x] 1.1a `uv` project, `src/` layout, Ruff + mypy + pytest config copied from Menagerist, Poe tasks (§7) 🧪
- [x] 1.1b `ci.yml` (two jobs: static, test) with pinned action SHAs; green on a trivial test
- [x] 1.1c prek config: file hygiene, ruff, conventional commits, gitleaks, `no-commit-to-branch`, actionlint
- [x] 1.2 FastAPI app factory, Jinja2 base, Pico, local assets, health page
- [x] 1.3 SQLAlchemy async + SQLite (WAL, `BEGIN IMMEDIATE`) and Alembic 🗄️
- [x] 1.4 `users` + `sessions` tables, argon2 hashing, login and logout, hashed server-side sessions, strict cookies 🗄️ 🧪
- [x] 1.5a CSRF by `Sec-Fetch-Site`/`Origin` on every unsafe method, login included (done with 1.4) 🧪
- [x] 1.5b Login throttle per username and IP, plus a device cookie giving known browsers a higher, finite per-account limit (PR #7) 🗄️ 🧪
- [x] 1.6a Admin bootstrap from env (no default credentials), first-run only 🧪
- [x] 1.6b Admin user management pages: create, disable and enable, make admin 🧪
- [x] 1.6c Private dashboard and navigation; two users see separate pages 👁️ DEMO
- [x] 1.6d Change password: ends other sessions and calls `AuthService.forget_devices` 🧪
- [x] 1.6e Logo, favicon and app icons (SVG source, 192 and 512 PNG, one maskable) in `web/static/img/`, shown in the nav and on the sign-in page; fixes the `/favicon.ico` 404 👁️ DEMO
- [x] 1.7 Extend the architecture tests as packages appear (providers, identity, matching)
- [x] ⛔ GATE: two users are isolated, 95% coverage on `auth/` (185 tests, `auth/` at 100%)

### WP-2 Watchlist
- [x] 2.1 TMDB provider with fixtures, `Protocol` in `providers/base.py` 🧪
- [x] 2.2 `people`, `productions`, `credits`, `external_identities` 🗄️
- [x] 2.3 Search UI (HTMX) for people, films and series (with posters and photos)
- [x] 2.4 `watches` + watch and unwatch, per-user isolation 🗄️ 🧪
- [ ] 2.5 Franchises with the Bond seed and franchise watches 🗄️ 👁️ DEMO (deferred: do it alongside WP-6, when a franchise watch has alerts to drive. The Bond film list is also needed by 7.4.)
- [x] ⛔ GATE: watches survive restart, 404 on another user's watch (met by 2.4; 2.5 is deferred)

### WP-3 Event browser
- [x] 3.1 Provider 1 parser against fixtures, `ListingResult.complete` logic 🧪 (Rostercon event page parser only; fetching and discovery come in 3.3 and 3.4)
- [x] 3.2 Convention, source, observation and appearance tables 🗄️ (`job_runs` is created with 3.4)
- [x] 3.3 Discovery service: idempotent upsert, provenance, prune observations 🧪
- [x] 3.4 Targeted refresh of what people watch (split into four chunks below). Rostercon is fetched per watched person, not swept in full.
  - [x] 3.4a Parsers for event rows and a person's full-events link, and name slugs 🧪
  - [x] 3.4b Polite fetch client: spacing, backoff on 429 and 503, and finding a person by asking for their page 🧪
  - [x] 3.4c `job_runs`, non-overlapping lock, and the refresh for watched people 🗄️ 🧪
  - [x] 3.4d Scheduler (daily) and an admin-only manual refresh
- [x] 3.5 UK events and guests pages, unknowns shown as unknown 👁️ DEMO
- [x] 3.6 Source health page; a parse failure keeps old data 🧪
- [x] ⛔ GATE: double refresh gives no duplicates (met by 3.4c's refresh-twice test)

### WP-4 Relevant appearances + identity
- [x] 4.1 Identity resolver (manual > shared ID > watched-name *Possible* > unresolved) 🧪
- [x] 4.2 `identity_decisions` + rejection persistence 🗄️ 🧪
- [x] 4.3 Matching service and `matches` table with `match_key` 🗄️ 🧪
- [x] 4.4 "My relevant appearances" page with match reasons 👁️ DEMO
- [x] 4.5 Admin review UI: confirm, reject, link, unlink, bulk confirm exact names
- [x] 4.6 Rematch is idempotent; the correction survives 3 re-syncs 🧪
- [x] ⛔ GATE: 95% coverage on `identity/` and `matching/`

### WP-5 Second source
- [x] 5.1 Provider 2 parser against fixtures 🧪
- [x] 5.2 Strict event dedupe and optional `convention_merges` 🗄️ 🧪
- [x] 5.3 Cross-source guest handling; conflicts and unknowns displayed
- [x] 5.4 Admin merge for uncertain event pairs 👁️ DEMO
- [x] ⛔ GATE: the same event from both fixtures shows once with two sources
- [ ] 5.5 Use Comiconomicon for real (added after the gate: nothing fetched it yet). The owner chose its search box, `/xhr/indexautocomplete.php`, to turn names into its ids (see `docs/decisions.md`).
  - [x] 5.5a Parse the search answers and match a name exactly 🧪
  - [x] 5.5b Polite fetcher for Comiconomicon (shared with Rostercon in `providers/polite.py`) and a cached lookup for watched people (`source_lookups`) 🗄️ 🧪
  - [ ] 5.5c Refresh from Comiconomicon for watched people (guest pages, then event pages), as a scheduled job and a button 🧪
  - [ ] 5.5d Watched films and series through their film pages, and health for the second source 🧪

### Added during the build (not in the original plan)
- [x] A1 Settings read from a `.env` file, with `.env.example`; tests never read it (#54)
- [x] A2 Startup: automatic database update with a safety copy first, never crash (a plain 503 page and a degraded `/health` instead), and backups: daily, at startup if the newest is old, on demand from the admin Source health page (#55) 🧪
- [x] A3 Docker image, Compose file, health check and `poe docker-*` tasks; running on the owner's PC behind `tailscale serve` (#56) 🧪
- [x] A4 Logo: a wavy trail tucked behind the star, an icon generator (`poe generate-icons`) and a test that the committed icons match the logo (PR #59, open) 🧪
- [x] A5 Move the UI from Pico to Bootstrap 5.3 (issue #50): one-row nav with an Admin dropdown, tables that fit a phone, dark mode 👁️ DEMO
- [ ] A6 Startup catch-up for the daily refresh 🧪
- [ ] A7 Move the older tests from injected clocks and `Clock` helpers to `time-machine` 🧪
- [ ] A8 GitHub issue templates (issue #51)
- [ ] A9 Time the refreshes: show each run's duration on the Source health page, and split it by step (lookup, person page, event list, event pages) with counts, separating time waiting on the spacing from time the site took to answer. Stored in the run's `summary` JSON (no migration). Per person is logged (`person_refreshed` with `duration_ms`), not stored. Do it after 5.5c so it covers both sources 🧪 👁️ DEMO The Source health warning "Some watched people have no single Rostercon page; skipped." should also name the people and say why (not found, or several found).
- [ ] A10 Link a watched person to a Rostercon page by hand: an admin pastes a Rostercon person page address for someone the automatic lookup could not find (seen live: Thomas F. Wilson and Kirsten Cooke were "not found", probably because Rostercon spells the name differently). The address is checked as a Rostercon `/en/people/` page of this host, fetched once to confirm it exists, and stored against the person. A refresh uses a stored link instead of guessing a slug, and a sync never replaces or removes it (only an admin can unlink). Shown on the person and the Source health page. Likely shares the lookup cache table from 5.5b with a `manual` flag 🗄️ 🧪 👁️ DEMO
- [x] A11 Review guests shows the source of each guest (Rostercon or Comiconomicon, with the source's own id), so two guests with the same name from different sources can be told apart 🧪

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
- [x] 8.1 Dockerfile and Compose, migrate-then-start (done early as A2 and A3)
- [x] 8.2a Backups, by SQLite's online backup API (it copies consistently while the app writes), kept in a folder; done early as A2
- [ ] 8.2b Restore test 🧪 (restore is documented in `docs/operations.md` but not yet covered by a test)
- [ ] 8.3 Two-user smoke test, security pass (cookies, `/docs` disabled)
- [ ] 8.4 README and `docs/decisions.md` cleanup 👁️ DEMO
- [ ] ⛔ GATE: fresh install and a restore both work

## 12. Status and handoff

**As of 2026-11-09.** WP-0 to WP-4 are done and merged. WP-5 is done through 5.5a (the parser for Comiconomicon's search answers); the gate (the same event from both fixtures shows once with two sources) is met. Startup robustness, backups and Docker are in. `uv run poe check` passes on the stack tip with 1202 tests and about 99% coverage; `auth/`, `identity/` and `matching/` are at 100% against the 95% gate. Open PRs, bottom up: #59 (logo), #57 (5.4, merge screen), #58 (5.5a), as one stack on `main`. #9 (Python 3.15 interpreter) is held.

### Running now
CastTrail runs in Docker on the owner's PC, reached over Tailscale at `https://sam-pc.tail0bed4.ts.net` (`tailscale serve` to `127.0.0.1:8002`). The database is in the named volume `casttrail_casttrail-data`, backups go to `./backups`, and settings are in `.env` (ignored by git). The Rostercon refresh is **off** (`CASTTRAIL_REFRESH_ENABLED=false`), so nothing contacts a source until the admin presses "Refresh now" or turns the timer on. Comiconomicon is not fetched yet. How to run, update, back up and restore: `docs/operations.md`.

### Done
- **WP-0 and WP-1:** tooling and CI (Ruff, strict mypy, pytest with branch coverage, archunitpython architecture tests, pytest-alembic, prek, required checks `CI passed` and `Hooks: prek (autofix)`, protected `main`, squash merge), app shell, async SQLite (WAL, `BEGIN IMMEDIATE`), auth (Argon2id, hashed sessions, strict cookies, CSRF by `Sec-Fetch-Site`, login throttle with a device cookie), admin bootstrap and user pages, dashboard, burger nav, logo and icons.
- **WP-2:** TMDB provider, search with posters and photos, watch and unwatch. Franchises (2.5) are deferred to WP-6.
- **WP-3:** Rostercon parser, polite fetcher, targeted refresh of what people watch, `job_runs` lock, daily timer and "Refresh now", Events and guest pages (unknowns shown as unknown), Source health page.
- **WP-4:** identity resolver, shared identity decisions, matches (Possible and Confirmed), "My appearances", admin review screen with bulk confirm.
- **WP-5:** Comiconomicon parser (events, guests, films and series), strict event dedupe, each source's own view and cross-source guests shown once, admin merge of uncertain pairs, search-answer parser.
- **Ops:** `.env`, automatic update with a safety copy, never-crash startup, backups, Docker, poe docker tasks, icon generator.

### Next
1. **5.5b to 5.5d** (fetch Comiconomicon: a polite fetcher and cached lookup, a refresh for watched people, then films and series), then **WP-6 ntfy** together with franchise and Bond watches (2.5), then WP-7 and the rest of WP-8.
2. Owner actions: merge #59, #57, #58 (bottom up); put a fresh TMDB key in `.env` (the one pasted into chat should be rotated); set Docker Desktop to start with Windows; copy `./backups` somewhere off the machine now and then.

### Decisions made since the plan was written
- Providers stay in-tree behind a `Protocol`; entry points deferred. Bond is an optional in-tree module, not a plugin.
- HTMX only, no JSON API. Own sessions and cookies instead of FastAPI Users.
- CSRF by `Sec-Fetch-Site` and `Origin` (no form tokens). A request with neither header is refused, so `curl` needs an `Origin` header.
- `SameSite=Lax`, not `Strict`, so a link from an ntfy alert opens the app signed in.
- HTTP client is `httpx2`. Server is Granian. Architecture rules are archunitpython tests.
- Only **confirmed** matches will alert; a name match is *Possible* until a person confirms it. Identity decisions are shared by all users and made on an admin-only screen.
- A target-only refresh: only what people watch is fetched, never a full sweep.
- Rostercon's details win for a merged event; other sources only fill gaps. A source's two events are never joined; a merge by hand cannot be undone. Comiconomicon guest lists are never "complete".
- The Comiconomicon search box is used by the owner's decision (recorded in `docs/decisions.md`).
- Docker: one container, database in a named volume (SQLite WAL is unreliable on a Windows bind mount), backups to a plain folder, published on `127.0.0.1:8002`.
- Startup: update the database automatically with a safety copy first (and not at all if the copy fails); a startup problem gives a plain 503 page and a degraded `/health`, not a crash.
- APScheduler stays in memory; a startup catch-up is preferred to a jobstore.
- Tests use `time-machine`. The UI uses Bootstrap 5.3 (vendored, not the 6.0 alpha), replacing Pico.

### Open items and reminders
- `AuthService.forget_devices` is called on password change and when an admin disables an account. Any later password reset or session revocation must call it too.
- The client address comes from `request.client.host`. Behind the Tailscale proxy that is the proxy, so the per-address sign-in limit is shared by everyone; it needs a trusted-proxy setting.
- `sessions.client_ip` and `user_agent` are stored but no page shows them yet.
- There is no password reset (an admin cannot set another user's password) and no way to remove admin rights. Both were left out on purpose.
- Renovate PR #9 (Python 3.15 interpreter) is open and held until there is more code to test against.
- Issues: #50 (Bootstrap), #51 (issue templates). Menagerist issues raised from this work: #297 (Renovate automerge settings) and #312 (device cookie and device-grouped sessions).
- PyCharm exports `VIRTUAL_ENV` pointing at a stray `venv/` folder, which makes uv warn. Repoint the interpreter to `.venv` and delete `venv/`.
- GitHub repo is public (existing `LICENSE` is MIT).
