# CastTrail: Plan v1

**Replaces:** HLD v0.12 and LLD v0.8, which are now background only. This is the single source of truth.
**Date:** 2026-11-07 · **Audience:** the developer and coding agents

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

Entry-point plugins, JSON API, digests, Bond ranking and bipartite assignment, collection sharing, extra providers, OIDC, advanced identity scoring.

**Installable PWA (optional).** Web app manifest, the 1.6e icons, a service worker that caches only static assets, and an install prompt. Notes: it needs HTTPS, so it follows WP-8; the service worker must never cache pages or responses behind a login, so it stays a shell and offers no offline data; and a tap on an ntfy alert should open the installed app (the `SameSite=Lax` decision already allows that link to arrive signed in). Do 1.6e first so the icon set is ready. Web Push (below) builds on it.

**More notification channels (planned after WP-6).** Each is its own slice behind the channel-agnostic outbox.
- *Email (SMTP):* an admin-set server, a per-user address, a plain-text body built from the same sanitised message, and a test button. It is only a new adapter.
- *Web Push to an installed PWA:* needs the PWA first (manifest, service worker, HTTPS), VAPID keys held as admin-set secrets, one stored push subscription per browser, and removal of a subscription when the push service answers 404 or 410. iOS delivers push only to a PWA added to the home screen.

Revisit these only after WP-8, each as its own slice.

## 10. Open questions (answer inside WP-0 or WP-1)

1. Which two convention sites, and are they permitted?
2. Final auth choice (custom sessions or FastAPI Users)?
3. Is Python 3.14 compatible with every dependency's wheels? If not, use 3.13.
4. (For WP-6) Which channels besides ntfy, and in what order: SMTP, Web Push?

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

## 12. Status and handoff

**As of 2026-11-08.** WP-0 and WP-1 are done. `uv run poe check` passes on `main` with 185 tests and about 99.9% coverage, and `auth/` is at 100% against the 95% gate. The WP-1 gate has not been formally reviewed by the project owner yet.

### Done (WP-0, and WP-1 chunks 1.1 to 1.7)
- Tooling and CI: Ruff, strict mypy, pytest with branch coverage, archunitpython architecture tests, pytest-alembic, prek hooks, `CI passed` and `Hooks: prek (autofix)` as the required checks. `main` is protected and PR-only, squash merge, auto-merge allowed.
- App shell: `create_app()`, `/health`, status page, local Pico and HTMX.
- Database: async SQLite (WAL, `BEGIN IMMEDIATE`), Alembic, migrations 0001 to 0004 (baseline, users and sessions, sign-in attempts, known devices).
- Auth: Argon2id passwords, hashed server-side sessions, strict cookie reader, `SameSite=Lax`, `__Host-Http-` prefix when Secure, CSRF check on every unsafe method, login throttle (5 per email, 20 per address, 15 minute window), device cookie raising the per-account limit to 20 for a known browser.
- Admin and accounts: first admin from `CASTTRAIL_ADMIN_EMAIL` and `CASTTRAIL_ADMIN_PASSWORD` only on an empty `users` table, admin pages at `/admin/users` (add, disable, enable, make admin; 404 for non-admins; no self-disable and no admin removal), private dashboard at `/`, navigation, change password at `/account/password`.
- Passwords are 12 to 256 characters when set. Changing one ends every other session, forgets remembered browsers, and counts wrong guesses at the current password towards the sign-in throttle.
- Public `/status` shows version and DB revision (it must work before login and before migration). `/health` discloses nothing.
- Logo, icons and favicon in `web/static/img/` (the icon set is ready for the optional PWA).
- Architecture rules cover `auth`, `identity` and `matching` (framework-free), `providers` (no database, auth or web), and a ban on `requests` and `httpx` by import scan.
- Renovate configured and running (autofix.ci app also installed).

### Next
1. **WP-1 gate review**, then WP-2 (Watchlist), starting with the TMDB provider (2.1). The TMDB API key and how it is configured are not decided yet.
2. WP-0 findings that matter later (see `docs/decisions.md`): Rostercon is primary and Comiconomicon secondary, neither grants explicit permission to scrape, and both parse from saved fixtures. Open: where Rostercon shows a cancelled guest (WP-3), and Rostercon returns 503 under load, so use a slow rate and a cache.

### Decisions made since the plan was written
- Providers stay in-tree behind a `Protocol`; entry points deferred.
- HTMX only, no JSON API.
- Own sessions and cookies instead of FastAPI Users.
- CSRF by `Sec-Fetch-Site` and `Origin` (no form tokens). A request with neither header is refused, so `curl` needs an `Origin` header.
- `SameSite=Lax`, not `Strict`, so a link from an ntfy alert opens the app signed in.
- HTTP client is `httpx2`. Server is Granian (`uv run poe serve`).
- Architecture rules are archunitpython tests, not Import Linter.

### Open items and reminders
- `AuthService.forget_devices` is now called on password change and when an admin disables an account. Any later password reset or session revocation must call it too.
- Client address comes from `request.client.host`. Behind a reverse proxy it needs a trusted-proxy setting (WP-8), or the per-address limit is shared by everyone.
- `sessions.client_ip` and `user_agent` are stored but no page shows them yet. A sessions page grouped by browser is a candidate now that 1.6 is done.
- There is no password reset (an admin cannot set another user's password yet) and no way to remove admin rights. Both were left out on purpose; add them when needed.
- Polish noted in the 1.6c demo: the Sign out button in the nav is heavier than the links beside it, and the Users table buttons are large.
- The `/status` page has no navigation, only a link back to the dashboard.
- Renovate PR #9 (Python 3.15 interpreter) is open and blocked. Hold it until there is more code to test against.
- Menagerist issues raised from this work: #297 (Renovate automerge settings, milestone v0.2026.11.3) and #312 (device cookie and device-grouped sessions, milestone Identity and ownership).
- PyCharm exports `VIRTUAL_ENV` pointing at a stray `venv/` folder, which makes uv warn. Repoint the interpreter to `.venv` and delete `venv/`.
- GitHub repo is public (existing `LICENSE` is MIT).
