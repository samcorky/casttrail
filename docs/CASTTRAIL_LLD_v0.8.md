# CastTrail — Low-Level Design & Implementation Plan

**Version:** 0.8 · **Date:** 2026-10-27 · **Status:** Proposed / ready for implementation review
**Parent:** [High-Level Design v0.12](./CASTTRAIL_HLD_v0.12.md) · **Audience:** Python developers and coding agents

> **Document contract:** The HLD is authoritative for product intent, user stories, feature scope, ranking philosophy, UI choices and delivery phases. This LLD specifies *how to implement* those decisions. Do not copy HLD prose into code comments or agent plans. Where the documents disagree, raise an ADR before coding rather than silently picking one.

**Naming:** CastTrail is the chosen project name. See HLD v0.12, “Project naming and availability”; public trademark and domain clearance remains outstanding. Python packages and plugin entry points use `casttrail` consistently.

## 0. Scope and decisions requiring validation

**Implement now:** WP-00–08 (foundation; metadata/watches; two convention providers; basic identity corrections; matching and ntfy; generic and Bond collections; hardening). **Defer:** Bond ranking/assignment, email/digests, sharing/comparison, sophisticated identity-review workflows, additional providers, organiser website templates, OIDC/Authentik, CLI, API tokens and general RBAC. Retain future-facing schemas/contracts where inexpensive, but do not implement deferred UI/services. HLD §3 and §16 define the updated MVP scope.

**LLD decisions (proposed):**
- One Uvicorn worker and one APScheduler instance; async SQLAlchemy/SQLite with Alembic.
- Core-owned persistence; plugins return observations and optional collection-specific computations. No plugin may write the database directly.
- The `casttrail-plugin-api` package defines typed DTOs and capability protocols; avoid an abstract base class hierarchy.
- One application-level `CurrentUser` dependency is shared by web and JSON API; authorization is checked in services, not only route decorators.
- One database-backed notification outbox; ntfy delivery for MVP, SMTP adapter deferred.
- Initially prefer conservative identity matching: auto-link only when an authoritative external identifier is already known; ambiguous name matches require review.
- All HTML routes render full pages or explicit partial routes. No HTTP calls from web handlers to the JSON API.

**Resolve before implementing the relevant slice:** public name/trademark clearance before release; FastAPI Users version/maintenance and DB token strategy compatibility; provider access permissions and available stable IDs; source-specific cancellation semantics; Bond eligibility evidence quality. Record each resolution in `docs/decisions/`.

## 1. Repo layout and dependency direction

Reference HLD §§4, 6–7, 12 and 15.

```text
casttrail/
├── pyproject.toml                 # workspace, dependencies, Poe, lint/type tools
├── uv.lock
├── alembic.ini
├── migrations/
├── docs/
│   ├── HLD.md                     # copy or link to parent HLD
│   ├── LLD.md                     # this document
│   └── decisions/                 # short ADRs
├── src/casttrail/
│   ├── main.py                    # app factory + lifespan/composition root
│   ├── config.py                  # pydantic-settings
│   ├── db.py                      # engine, session dependency, migrations wiring
│   ├── auth/                      # FastAPI Users adapters, users, sessions
│   ├── domain/                    # shared enums, pure policy functions
│   ├── identity/                  # canonical identity + decisions
│   ├── discovery/                 # ingest/upsert + sync state
│   ├── watches/                   # user-owned watch service
│   ├── matching/                  # appearance → watch matches
│   ├── collections/               # generic user-owned collections
│   ├── notifications/             # outbox, channels, templates
│   ├── jobs/                      # APScheduler registration + job status
│   ├── plugins/                   # entry-point discovery + registry
│   ├── api/v1/                    # JSON adapters only
│   └── web/                       # routes, templates, static assets
├── packages/
│   ├── casttrail-plugin-api/src/casttrail_plugin_api/
│   ├── casttrail-tmdb/src/casttrail_tmdb/
│   ├── casttrail-rostercon/src/casttrail_rostercon/
│   ├── casttrail-comiconomicon/src/casttrail_comiconomicon/
│   └── casttrail-bond/src/casttrail_bond/
└── tests/
    ├── architecture/
    ├── unit/
    ├── integration/
    └── fixtures/
```

**Import rules:** plugin API imports no app/plugin package; plugins import plugin API, third-party libraries and their own code only; core does not statically import concrete plugins; `domain` imports neither FastAPI nor SQLAlchemy; `api` and `web` may import services, never the reverse. Use Import Linter and a package-manifest dependency test. Allow composition root to instantiate providers via entry points.

**Avoid creating layers solely for their names:** no mandatory repository per entity, mediator, command bus, event bus or per-endpoint use-case class. SQLAlchemy access in focused services is acceptable.

## 1A. Release, plugin compatibility and migration policy

**Version sources of truth**

- **CastTrail application:** use `calver-scm` (SCM-tag-derived). Development release examples `0.2026.10.0`, `0.2026.10.1`; actual tag format/configuration must be checked against the installed tool. Do not hard-code a release version in source or infer a version from WP number. Provide `uv run poe version` (or an equivalent documented task), package metadata, and a status view with application version and active Alembic revision.
- **`casttrail-plugin-api`:** SemVer, initially `0.x`; define compatibility/breaking-change policy and type-checked provider contracts. Explicit dependency constraints in provider packages; reject or report incompatible plugins at startup with actionable diagnostics. Keep compatibility tests/fixtures for every shipped provider.
- **Provider plugins:** independently released (CalVer is preferred but not mandatory); plugin package version does not substitute for declared plugin API compatibility.
- **Database:** Alembic revision chain is the sole schema version authority. Never name revisions after CalVer releases or WP identifiers as if they were equivalent.

**Migration implementation rules**

1. WP-00 configures the async SQLAlchemy metadata, Alembic environment and migration/test commands; no business tables are required. Keep an empty baseline revision only if it makes the workflow simpler.
2. Each WP creates only schema needed for its visible deliverable. The §2 table inventory is a *future target*, not an instruction to create everything upfront. Each WP handoff names added/changed/unchanged tables and revision IDs.
3. Run migrations explicitly before serving requests in deployment; avoid `create_all()` for production schema management. Test both empty-database bootstrap and upgrades from a populated previous-checkpoint fixture. Include downgrade tests where safe; if irreversible, document backup/restore instead.
4. Use Alembic batch operations for SQLite table rebuilds, enable SQLite foreign keys on every connection, and test data preservation, constraints, and rollback/restore behaviour. Do not promise universally reversible SQLite downgrades.
5. Core owns migrations. Convention and TMDB providers return typed DTOs and never execute DDL or create their own core tables. Future plugin-owned storage requires a separately reviewed migration/compatibility design.
6. WP-05 should require no new core tables; a schema change is a design-review trigger to determine whether WP-03/04 encoded provider-specific assumptions.

## 2. Persistence schema and invariants

Reference HLD §§8, 10, 13. Use UUID primary keys (`uuid.UUID` in Python; SQLite-compatible storage), timezone-aware UTC timestamps at application boundaries, explicit foreign keys, uniqueness and indexes. Prefer string enums with DB-level validation where practical. Schema below is a **target-state inventory**, not a complete Alembic migration or WP-00 creation list. Implement only the relevant rows in each slice.

| Table | Essential fields | Constraints / indexes |
|---|---|---|
| `users` | `id`, `email`, `hashed_password`, `is_active`, `is_superuser`, `is_verified`, `created_at` | unique normalized email; compatible with selected FastAPI Users adapter |
| `access_tokens` | FastAPI Users database-token adapter fields | use supported adapter schema; do not hand-roll token storage |
| `people` | `id`, `display_name`, `birth_date?`, `death_date?`, `created_at`, `updated_at` | indexes on normalized name; dates nullable |
| `productions` | `id`, `title`, `kind`, `release_date?`, `created_at` | `kind` film/series; do not dedupe by title alone |
| `credits` | `id`, `person_id`, `production_id`, `role_type`, `character?`, `source_identity_id?` | index `(production_id, person_id)`; support multiple roles |
| `external_identities` | `id`, `entity_kind`, `entity_id?`, `provider`, `external_id`, `display_name?`, `resolution_method`, `verified_at?` | unique `(provider, entity_kind, external_id)`; unresolved allowed |
| `identity_decisions` | `id`, `external_identity_id`, `candidate_entity_id`, `decision`, `reason?`, `actor_user_id?`, `created_at` | index identity + candidate; immutable decision log or explicit reversal |
| `conventions` | `id`, `name`, `starts_on`, `ends_on`, `country_code`, `city?`, `venue?`, `website?` | country/date indexes; avoid fuzzy auto-merges |
| `appearances` | `id`, `person_id?`, `convention_id`, `status`, `first_seen_at`, `last_seen_at`, `confirmed_at?` | unique canonical person+event only when confidently resolved; unresolved rows retain source identity |
| `source_observations` | `id`, `provider`, `source_key`, `entity_kind`, `payload_json`, `content_hash`, `fetched_at`, `source_url` | unique `(provider, source_key, content_hash)`; preserve evidence |
| `appearance_sources` | `appearance_id`, `source_observation_id`, `source_status` | unique pair; supports provenance |
| `watches` | `id`, `user_id`, `target_kind`, `target_id`, `created_at` | unique `(user_id, target_kind, target_id)`; index user |
| `collections` | `id`, `user_id`, `kind`, `name`, `settings_json` | index `(user_id, kind)` |
| `collection_items` | `id`, `collection_id`, `production_id`, `status`, `notes?` | unique `(collection_id, production_id)` |
| `signatures` | `id`, `collection_item_id`, `person_id`, `signed_on?`, `convention_id?`, `notes?`, `in_person`, `created_at` | unique signer **per collection** enforced in service/transaction; see below |
| `eligibility_overrides` | `id`, `collection_id`, `production_id`, `person_id`, `eligible`, `reason`, `created_at` | unique `(collection_id, production_id, person_id)` |
| `matches` | `id`, `user_id`, `appearance_id`, `watch_id?`, `reason_code`, `state`, `created_at` | unique `(user_id, appearance_id, watch_id, reason_code)` with null-safe design |
| `notification_events` | `id`, `event_key`, `event_type`, `appearance_id?`, `payload_json`, `created_at` | unique `event_key` |
| `user_notifications` | `id`, `event_id`, `user_id`, `read_at?`, `superseded_at?` | unique `(event_id, user_id)` |
| `notification_channels` | `id`, `user_id`, `kind`, `destination`, `config_json`, `enabled`, `verified_at?` | index user; never expose secrets in responses |
| `notification_preferences` | `user_id`, `event_type`, `channel_id`, `delivery_mode` | unique `(user_id, event_type, channel_id)` |
| `notification_deliveries` | `id`, `user_notification_id`, `channel_id`, `status`, `attempts`, `next_attempt_at`, `last_error?`, `sent_at?` | unique `(user_notification_id, channel_id)`; pending index |
| `job_runs` | `id`, `job_key`, `status`, `started_at`, `finished_at?`, `items_seen`, `error?` | job/status/time indexes |

**Important schema clarifications:**
1. **Identity:** `external_identities.entity_id` may be null while unresolved; never infer a match solely from equal display names. Identity decisions are separate from identity status; rejection of candidate A must not invalidate the external identity.
2. **Appearances:** unresolved guest observations must remain representable without inventing a canonical `Person`. Prefer a nullable `person_id` plus source-identity reference and prevent confirmed matching until resolved.
3. **Source observations:** `source_key` should be stable across syncs; `content_hash` tracks revisions. Store minimal normalized evidence, not full scraped HTML indefinitely.
4. **Unique Bond signer:** a signature is attached to a collection item, but a signer cannot count for two items in the **same collection**. Implement this with a transactionally checked constraint or a schema design that makes `(collection_id, person_id)` unique (e.g. denormalize collection_id into signatures with FK consistency validation). A simple application-only precheck is insufficient under concurrent writes.
5. **Match uniqueness:** nullable `watch_id` breaks straightforward SQLite unique semantics. Use distinct match reason/source keys or a deterministic non-null `match_key` with unique `(user_id, match_key)`.
6. **User isolation:** `collection_item_id` alone is not proof of ownership. Resolve the collection owner on every read/mutation, including signatures and overrides.
7. **Schema evolution:** Alembic revisions for each feature slice, plus migrations tested against a populated fixture DB.

## 3. Application services and transaction boundaries

Reference HLD §§4, 8–9. Services receive an `AsyncSession` and only required collaborators via constructors. API/web routes convert transport payloads to commands and results.

```python
@dataclass(frozen=True)
class ResolveIdentity:
    external_identity_id: UUID
    canonical_person_id: UUID
    decided_by: UUID
    reason: str | None = None

class IdentityService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def accept(self, command: ResolveIdentity) -> None:
        """Reject conflicting candidates, update mapping and queue rematch in one transaction."""
        ...
```

| Service | Owns | Must not own |
|---|---|---|
| `IdentityService` | link/unlink/reject, manual precedence, identity audit, affected-appearance rematch request | provider HTTP calls |
| `DiscoveryService` | validate observation, idempotent upsert, source provenance, sync status | user-specific alert delivery |
| `WatchService` | CRUD for watches scoped to `user_id`, production expansion rules | scraping |
| `MatchingService` | compute why an appearance matches a user's watches; materialize deterministic match keys | delivery adapters |
| `CollectionService` | collection ownership, item/signature CRUD, eligibility overrides | Bond-specific scoring formula |
| `BondRankingService` (plugin capability) | pure ranking/assignment over provided snapshots | direct SQL or global identity mutation |
| `NotificationService` | create logical user notifications, preferences, outbox | HTTP endpoint rendering |
| `DeliveryService` | claim due deliveries, retry/backoff, call ntfy/SMTP adapters | match discovery |
| `JobService` | schedule/run locks, status and manual trigger | implementation of provider parsing |

**Transactions:** commit a source batch and its provenance together; commit a manual identity correction and invalidation markers together; commit event/user-notification/delivery records together. Perform network calls *outside* DB transactions. Reprocessing must be safe after crash/retry.

## 4. Plugin API and discovery contract

Reference HLD §§6–7 and incremental checkpoints in HLD §16. The API package contains **only** stable types and interfaces. Plugin-specific config belongs in each plugin. Prefer capability registration to a single exclusive plugin-type enum.

```python
# casttrail_plugin_api/contracts.py
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from pydantic import BaseModel, HttpUrl

class AppearanceStatus(StrEnum):
    ANNOUNCED = "announced"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

class GuestObservation(BaseModel):
    source_key: str
    guest_name: str
    source_url: HttpUrl
    provider_person_id: str | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    attendance_days: list[date] | None = None
    autograph_available: bool | None = None
    autograph_price: Decimal | None = None
    status: AppearanceStatus | None = None

class ConventionObservation(BaseModel):
    source_key: str
    name: str
    country_code: str
    starts_on: date
    ends_on: date
    source_url: HttpUrl

class ConventionProvider(Protocol):
    @property
    def provider_id(self) -> str: ...
    async def discover(self) -> AsyncIterator[ConventionObservation]: ...
    async def guests(self, event_key: str) -> AsyncIterator[GuestObservation]: ...

class Plugin(Protocol):
    def register(self, registry: "PluginRegistry") -> None: ...
```

`PluginRegistry` should register named capabilities (`metadata`, `conventions`, `collections`) with duplicate-ID validation. Discover installed factories via `importlib.metadata.entry_points(group="casttrail.plugins")`; instantiate once in lifespan, inject a shared configured `httpx.AsyncClient` where needed, and close resources on shutdown. **Do not** let plugin import-time side effects register jobs or touch DB.

**Source adapter contract:** stable keys, bounded HTTP requests, pagination, rate limits, identifiable failures, parser fixtures, no bypass of robots/terms. Distinguish `not_found`, `temporarily_unavailable`, `cancelled` and `removed_from_listing`; do not interpret missing HTML as cancellation.

## 5. Identity resolution and correction algorithm

Reference HLD §§3 (US-05/06/20), 8 and 11.

```text
Ingest guest observation
  → upsert external identity by (provider, person, external_id/source_key)
  → existing manually verified link? use it
  → known stable cross-provider ID already linked? propose or resolve if unambiguous
  → candidate search by name/credits? create review suggestion only
  → no candidates? unresolved
  → only resolved identity enters confirmed appearance matching
```

**Manual correction (`POST /api/v1/admin/identities/{id}/resolve`):** validate admin → ensure canonical target exists → record acceptance and rejected prior candidate → remap identity → invalidate affected matches/opportunities → mark incorrect user notifications superseded → schedule deterministic rematch. Use an optimistic concurrency version or row-level transaction guard appropriate for SQLite; return `409` for stale review decisions.

**Never:** silently override a manual decision on sync, automatically merge people by name, or delete delivered notification history. Provide `GET /api/v1/admin/identities?status=unresolved` and a Jinja2 review form using the same service.

**Tests:** wrong TMDB association corrected; rejected candidate stays excluded after three syncs; name change preserves verified link; competing manual corrections yield conflict; rematch is idempotent; unresolved source identity generates no confirmed alert.

## 6. Discovery, matching and event generation

Reference HLD §§9 and 14. **One provider pipeline** for scheduler and admin Run Now:

1. `JobService` obtains non-overlap lock; creates `job_runs` row.
2. Provider fetches source pages with timeouts/rate limits and yields validated DTOs.
3. `DiscoveryService` upserts observations, canonical conventions and appearances; records provenance.
4. Identity resolution handles only stable identifiers automatically; ambiguous candidates enter review.
5. For changed/resolved appearances, `MatchingService` evaluates users' watches and (later) Bond opportunities.
6. Generate deterministic event keys, e.g. `appearance:new:{appearance_id}:{revision}`; insert outbox records using uniqueness constraints.
7. Commit, update job status and release lock. Network delivery occurs separately.

**Watch semantics:** person watch matches direct appearances; production watch expands to linked credited people; curated franchise watch expands to included productions. Store `reason_code` and evidence so UI can explain matches. Uncredited/manual eligibility should be evaluated by the Bond extension, not silently added to globally sourced credits.

**Event state:** `discovered` → `confirmed` → `cancelled`/`corrected` when supported by evidence. `first_seen_at` is not `announced_at`. Source deletion is not proof of cancellation. A correction can supersede old alerts but does not erase them.

**Idempotency:** unique source observations + canonical keys + deterministic event keys + user notification uniqueness. Include a repeat-sync integration test: same fixtures imported twice result in no extra alerts.

## 7. Bond collection extension

**MVP:** only film set, signature tracking, independent ownership and unique in-person signer constraint. Urgency scoring, scarcity data and maximum matching below are post-MVP designs. Generic collections belong in core and must work without this plugin.


Reference HLD §10 for eligibility, 25/27 films, ranking weights and matching philosophy. The plugin consumes **read-only snapshots** supplied by core and returns scored items/opportunities; it does not own tables or SQLAlchemy sessions.

```python
class BondFilmScore(BaseModel):
    production_id: UUID
    score: float
    explanation: list[str]
    evidence_quality: Literal["high", "medium", "low", "unknown"]

class BondRanking(Protocol):
    def rank_films(
        self, collection: CollectionSnapshot, evidence: BondEvidenceSnapshot
    ) -> list[BondFilmScore]: ...
```

**Invariants:** each collection has its own completed films, signer usage, overrides and weights; only verified in-person signatures count; 25-film default with opt-in non-Eon films; unknown survival/convention data is *unknown*, not zero. Weights validated to a normalized sum and ranking stable under equal inputs. Guest-to-film assignment uses maximum bipartite matching for unique signers; suggested allocations are never persisted as actual signatures without user action.

**Seed data:** curated Bond film IDs and release dates with source attribution; never rely on title matching. Keep cast/crew eligibility configurable and preserve manual override reasons.

**Tests:** independent father/son collections; same person can sign once for each collector; one collector cannot assign same signer to two films; manual credit override; low-quality evidence reduces confidence; deterministic scoring; overlapping actor-to-film choices maximize number of distinct completed posters.

## 8. Auth, ownership and security

Reference HLD §13. FastAPI Users local accounts, email/password, database-backed cookie tokens, no public registration. Build a bootstrap admin procedure through a **documented one-off setup step** (not a general application CLI): environment-based initial admin setup at first startup or an explicitly controlled migration/setup command; do not ship default credentials. Prefer the supported FastAPI Users password hashing/session adapters; verify versions before coding.

**Routes:** `/login` and `/logout` browser flows; admin user management under `/admin/users`; JSON auth routes under `/api/v1/auth/*` only if needed by API clients. Use secure `HttpOnly`, `SameSite=Lax`, `Secure` cookies in production; HTTPS via Traefik. Cookie-authenticated state changes require CSRF protection (including HTMX forms). Rate-limit login attempts. Never expose password hashes or session tokens in JSON schemas.

**Service-level access pattern:**

```python
async def rename_collection(
    session: AsyncSession, *, collection_id: UUID, user_id: UUID, name: str
) -> None:
    collection = await session.scalar(
        select(Collection).where(
            Collection.id == collection_id,
            Collection.user_id == user_id,
        )
    )
    if collection is None:
        raise NotFoundError("Collection not found")
    collection.name = name
```

Use `404` for missing **or not-owned** personal resources to avoid disclosure. Shared identity edits and provider management require admin. Phase 5 `collection_shares` grants view-only rights; no implied access through family relationship or matching email. Defer OIDC and trusted-proxy auth until explicitly requested.

## 9. Notification delivery contract

**MVP:** ntfy only, one enabled destination per user, immediate alerts and durable retries. SMTP and per-event/channel preferences are later extensions; examples below must not be treated as MVP acceptance criteria.


Reference HLD §13. Core adapters: `NtfyChannel` and `SmtpChannel`, each implementing `send(destination, message)`; do not create an external plugin package for them yet.

**On match:** create `notification_events` → `user_notifications` → channel-specific `notification_deliveries` in one transaction. The worker claims due deliveries, sends outside transaction, then records result. Backoff (example) 1m, 5m, 30m, 2h, then failed; admin can retry. Network timeout may cause duplicate external delivery: promise **idempotent scheduling**, not exactly-once delivery.

**Security:** ntfy topic names validated, server URL configured by admin (not arbitrary per-user URLs), ntfy credentials kept out of responses/logs; SMTP server and credentials from settings; users configure only destinations. Test channel before enabling; email ownership verification can be a later step, but avoid allowing unverified arbitrary third-party email sends. Provide per-user event-type/channel preferences; digest mode deferred unless implemented explicitly.

**Example event payload:**

```json
{
  "type": "appearance.new",
  "appearance_id": "<uuid>",
  "person_id": "<uuid>",
  "convention_id": "<uuid>",
  "reason_codes": ["watched_person", "bond_unsigned_film"],
  "source_url": "https://example.org/guest"
}
```

## 10. HTTP contracts and HTMX composition

Reference HLD §§11–12. JSON endpoints use Pydantic schemas and stable response codes; web endpoints use Jinja2 and explicit HTMX partials. `/api/v1` is versioned; `/openapi.json` and `/docs` generated by FastAPI.

| Endpoint | Method | Request / response | Auth | Phase |
|---|---|---|---|---|
| `/api/v1/people` | GET | `q`, `limit`, `offset` → `Page[PersonOut]` | user | 1 |
| `/api/v1/productions` | GET | `q`, `kind` → `Page[ProductionOut]` | user | 1 |
| `/api/v1/watches` | GET/POST | `WatchCreate` / `list[WatchOut]` | user scoped | 1 |
| `/api/v1/watches/{id}` | DELETE | `204` | owner | 1 |
| `/api/v1/conventions` | GET | country/date filters → page | user | 2 |
| `/api/v1/appearances` | GET | filters → page, source + reason | user | 2 |
| `/api/v1/admin/identities` | GET | status/provider filters | admin | 2 |
| `/api/v1/admin/identities/{id}/resolve` | POST | `IdentityResolutionIn` → `IdentityOut` | admin | 2 |
| `/api/v1/admin/jobs/{job_key}/run` | POST | `202` + job run ID, `409` if running | admin | 2 |
| `/api/v1/notification-channels` | GET/POST | redacted `ChannelOut` | user scoped | 3 |
| `/api/v1/collections` | GET/POST | `CollectionCreate` / page | user scoped | 4 |
| `/api/v1/collections/{id}/items` | GET | `list[CollectionItemOut]` | owner | 4 |
| `/api/v1/collections/{id}/signatures` | POST | `SignatureCreate` → `SignatureOut` | owner | 4 |
| `/api/v1/collections/{id}/opportunities` | GET | ranked, evidence-qualified results | owner | 4 |
| `/api/v1/collections/{id}/shares` | GET/POST/DELETE | view-only grants | owner | 5 |

**Error envelope** (JSON): `{"error":{"code":"not_found","message":"...","request_id":"..."}}`. Use `400` invalid workflow state, `401` unauthenticated, `403` forbidden admin action, `404` unknown/not-owned resource, `409` conflicts, `422` validation. Pagination capped at 100. Validate all filters and avoid N+1 queries.

**HTML example:**

```html
<form hx-get="/people/results" hx-target="#people-results" hx-trigger="input changed delay:300ms from:#search">
  <input id="search" name="q" type="search" placeholder="Search people" aria-label="Search people">
</form>
<section id="people-results" aria-live="polite">
  {% include "people/_results.html" %}
</section>
```

Use a consistent `base.html`, Pico CSS, locally served Lucide sprite, template macros for icon, status badge and pagination. Partial routes return only fragments; full routes return complete documents. Preserve focus and announce errors accessibly. Avoid a frontend build step unless unavoidable.

## 11. Scheduler, operations and failure modes

Reference HLD §14. Register APScheduler once in FastAPI lifespan; **one worker only**. Define bounded tasks: convention discovery (daily), appearance refresh (e.g. 6h), notifications (1m), stale review reminders (optional). Intervals are configuration, not guaranteed freshness. A job lock prevents concurrent manual and scheduled runs of the same key. Expose last attempt/success/error and item counts in admin UI.

**Operational baseline:** Docker Compose volume for SQLite and local assets; health endpoint checks app responsiveness and DB; no external Redis/Postgres requirement. `WAL`, busy timeout, graceful shutdown, database backup instructions, migration before app start. Avoid automatically running potentially destructive migrations concurrently with multiple processes.

**Failure rules:** provider HTTP 429 → respect Retry-After; parser drift → mark provider degraded and keep previous data; DB lock → bounded retry; ntfy/SMTP unavailable → retain pending outbox; identity conflict → review queue; missing guest listing → do not cancel; scheduler restart → pending deliveries recover from DB. A single failed provider must not erase other providers' observations.

## 12. Testing and architecture enforcement
### Engineering quality gates (mandatory)

CastTrail targets Python 3.14 with complete annotations on application and plugin public interfaces and all functions/methods (including return types). Enforce `ruff check`, `ruff format --check`, and `mypy --strict` across every workspace package; narrow third-party typing exceptions must be documented and scoped, never blanket `ignore_errors`. Use typed Pydantic DTOs at boundaries and avoid `Any` unless justified.

Use pytest with branch coverage (`pytest-cov`, `--cov-branch`), reporting **both repository-wide and application-area coverage**. Minimum branch-coverage targets: **90% overall**; **95%** for identity/reconciliation, appearance matching/deduplication, notification rules/outbox/retries, authentication/authorization, generic collection ownership, and Bond-only signer rules/ranking; **90%** scheduling; **85%** provider parsing/adapters, database adapters, and JSON/HTMX presentation handlers. Exclude migrations, generated code, and pure declarative configuration only with an explicit justification. These thresholds apply to executable code in each area, not just the test suite associated with it.

Coverage is a supporting metric, not proof of correctness. Require negative cases, concurrency/idempotency, two-user isolation, ambiguous source identities, unavailable providers, and CSRF/permission checks. CI fails on Ruff, strict mypy, architecture tests, failing tests, overall coverage or any area below its threshold. If a new application area has little code, require tests immediately rather than hiding it behind global coverage.


Reference HLD §15. Keep test pyramid lean:

- **Unit:** matching, ranking, signer uniqueness policy, identity decision precedence, notification event-key creation.
- **Integration (SQLite temp DB):** migrations, user isolation, corrected identity/rematch, double-import idempotency, notification retries, job overlap.
- **Adapter fixture tests:** saved representative source HTML/JSON; no live network in CI; mock HTTPX.
- **HTTP tests:** JSON response schemas, `401/403/404/409`, CSRF, HTMX partial vs full-page responses.
- **Architecture:** Import Linter boundaries, entry-point loading, plugin manifest dependency checks, mypy/ruff.

### Tooling reuse and rollout

Use **Menagerist's `feature/initial-implementation` branch as a read-only reference**. Relevant source files are its root `pyproject.toml` (Poe task groups, `prek` setup, lint/type/test tasks), `.pre-commit-config.yaml`, `backend/pyproject.toml` (Ruff, mypy, pytest and coverage settings), `backend/scripts/check_component_coverage.py` (per-component coverage reporting/checking), and `codecov.yml` (component coverage configuration). Copy only the useful patterns; update paths, imports, package names, thresholds and exclusions. Do not copy Menagerist's frontend/Playwright/Postgres build and test infrastructure into this Jinja2/SQLite application. Review the original code and licence before reuse.

**WP-00 (immediate):** add `ruff`, `mypy`, `pytest`, `pytest-cov`, `coverage`, `poethepoet`, and `prek` to the development environment; strict type and lint configuration; a small `.pre-commit-config.yaml` run by `prek`; Poe tasks for `format`, `format-check`, `lint`, `typecheck`, `test`, `coverage-status`, `coverage-check`, and `check`; CI running the baseline gates. Prefer `prek` as the hook runner (rather than running two competing hook systems). `poethepoet` is a development task runner, **not** an application CLI. Use `uv run poe ...` and `uv run prek ...`; no global install required. Check installed tool versions for Python 3.14 support.

**WP-01 (first authenticated vertical slice):** port/adapt Menagerist's `check_component_coverage.py` and coverage config into `scripts/check_component_coverage.py`. Enforce **glob-based groups** from measured source paths with branch coverage, including root application and workspace plugin packages. Use `coverage report --include=<glob> --fail-under=<target>` for simple stable groups where sufficient; use the script for dynamic glob sets, overlap resolution, status output and strict CI validation. Maintain an overall 90% gate. Do not let empty matching groups pass silently once the corresponding feature is implemented; before that point mark groups *pending* explicitly.

**WP-02 onward:** add or activate coverage rules as each module/package arrives. Before accepting a work package, its applicable group(s) must meet the threshold. WP-03 and WP-05 activate provider-adapter checks; WP-04 identity and matching; WP-06 notifications; WP-07 generic and Bond collection rules; WP-08 revalidates all packages and the full suite. Each PR must pass Ruff, strict mypy, tests, architecture boundaries and applicable coverage gates. Keep unit/integration tests deterministic, with source fixtures rather than live network access.

**Proposed group definitions (adapt globs to actual code layout in WP-01):**

| Group | Source glob(s), illustrative | Branch minimum | Activated |
|---|---|---:|---|
| Identity | `src/casttrail/**/identity/**/*.py` | 95% | WP-04 |
| Matching | `src/casttrail/**/matching/**/*.py` | 95% | WP-04 |
| Notifications | `src/casttrail/**/notifications/**/*.py` | 95% | WP-06 |
| Auth / permissions | `src/casttrail/**/auth/**/*.py` | 95% | WP-01 |
| Generic collections / ownership | `src/casttrail/**/collections/**/*.py` | 95% | when implemented |
| Bond plugin rules | `packages/casttrail-bond/**/*.py` | 95% | WP-07 |
| Scheduling | `src/casttrail/**/scheduling/**/*.py` | 90% | WP-03/04 |
| Provider adapters | `packages/casttrail-{tmdb,rostercon,comiconomicon}/**/*.py` (expand explicitly) | 85% | respective WP |
| DB adapters | `src/casttrail/**/db/**/*.py` | 85% | WP-01 |
| Web / JSON API | `src/casttrail/**/{web,api}/**/*.py` (expand explicitly) | 85% | WP-01 |
| All production source | `src/casttrail/**/*.py`, `packages/*/src/**/*.py` | 90% | WP-01 |

These globs are **examples, not an assumed final directory layout**. Resolve overlapping matches deterministically, ensure every production file is included in the overall gate, and test the checker itself with fixture coverage data (missing group, overlap, branch totals, Windows path separators, plugin package paths, rounding and strict failure). In `coverage.py`, branch percentage is based on combined statement and branch opportunities, not merely branch arcs. Avoid claiming pure branch-only percentage without a separate calculation. The `coverage report --include` mechanism can enforce per-glob minimums directly; a script adds useful group aggregation and diagnostics, as Menagerist already demonstrates.

**Task interface:** `uv run poe lint`, `uv run poe format-check`, `uv run poe typecheck`, `uv run poe test`, `uv run poe coverage-status`, `uv run poe coverage-check`, `uv run poe check`, `uv run prek run --all-files`. Configure `check` to actually run tests and generate current coverage data before checking thresholds; never gate against a stale `.coverage` file. Keep the Poe task graph straightforward and portable to Windows and Linux.

## 13. Developer work packages (agent-ready; independently usable increments)

### Schema changes and migration acceptance per work package

The names below are *proposed*, and may be refined when implementing each WP. A table is not mandatory merely because it appears in the target-state inventory. Every WP must document its actual Alembic revision(s), new/changed indexes and constraints, migration test fixtures, and what remains intentionally unimplemented.

| WP | Minimum tables introduced or changed | Migration acceptance | User-visible result |
|---|---|---|---|
| **00 — Running shell** | None. Configure engine, metadata, Alembic and migration command. | Empty DB starts/migrates; migration status visible; no speculative business schema. | Home, health and installed plugins. |
| **01 — Personal app** | `users`; `access_tokens` only if the selected supported auth strategy requires them. | Fresh install and upgrade from WP-00; test existing account login after migration. | Separate secure logins and private dashboard. |
| **02 — Watchlist** | `people`, `productions`, `credits`, `external_identities`, `watches`; add only actually needed indexes. | Unique external IDs and watch ownership tested on migrated populated DB. | Search TMDB and manage persistent watches. |
| **03 — Event browser** | `conventions`, `appearances`, `source_observations`, `appearance_sources`, `job_runs` or equivalent minimal sync-state table; reuse `external_identities`. | Repeat imports idempotently; retain historical evidence; migrate populated WP-02 data. | Browse first provider's events/guests with provenance. |
| **04 — Relevant appearances** | `identity_decisions` and `matches` only if persisted matching is necessary; add correction/lock metadata as needed. | Manual decisions survive re-import; unresolved identities do not falsely match. | Personal relevant-appearance view and correction UI. |
| **05 — Second source** | **No new core tables expected.** | Both providers ingest through existing contracts; duplicate event/guest merges safely; existing rows preserved. | Two-source event browser with provenance and conflicts. |
| **06 — ntfy alerts** | Minimal `notification_events`, `user_notifications`, `notification_deliveries`, and one ntfy destination per user (e.g. `notification_channels`). No preference matrix yet. | Outbox idempotency, retry and crash recovery; migrated users retain watches. | Configure ntfy and inspect delivery outcomes. |
| **07 — Collections** | `collections`, `collection_items`, `signatures`; `eligibility_overrides` only if needed for MVP Bond eligibility corrections. | Ownership and transaction-safe per-Bond-collection signer uniqueness; migration preserves alerts. | Generic autographs and separate Bond poster collections. |
| **08 — Deployable MVP** | No new business tables expected; only justified fixes. | Fresh bootstrap, upgrade from prior release, SQLite backup/restore, migration failure recovery. | Deployable and recoverable complete MVP. |

**Release cadence:** WPs are development checkpoints, not version numbers. Tag CalVer releases when a tested usable increment is ready; a release may include multiple WPs or a WP may span releases. Schema revision(s) travel with the release but are tracked independently. Never assume one migration per WP.



**Golden rule:** each WP ends in a working, browser-accessible increment, not just infrastructure. Ship the smallest end-to-end UI for that WP (Jinja2/HTMX + essential JSON), show an empty/loading/error state, persist what users change, and include a repeatable acceptance walkthrough. Run `uv run poe check` and the relevant per-area coverage gates; record screenshots or fixture-based verification plus `docs/handoffs/WP-XX.md` findings. **Never depend on live scrapes in CI.** Keep the application runnable throughout, with no placeholder route that silently succeeds.

| Package | Deliverable and end-of-package user journey | Dependencies | Acceptance / demonstration |
|---|---|---|---|
| **WP-00 — Running shell** | uv workspace, typed plugin API and entry points, FastAPI, SQLite/Alembic tooling (no domain tables), Jinja2/Pico shell, health/status page, Ruff/strict mypy/pytest/Poe/prek/CI | — | Start server, open home and status, see installed plugin list; all gates pass |
| **WP-01 — Personal app** | Admin bootstrap, local login/logout, private dashboard, CSRF, ownership, port Menagerist glob-based coverage checker | WP-00 | Two accounts sign in independently, cannot see each other's private pages; coverage checker reports current data |
| **WP-02 — Watchlist** | TMDB plugin, people/film/TV search, credits, TMDB external identities, watch/unwatch UI and JSON | WP-01 | Watch *Warehouse 13* or *Top Secret!* and see it persisted after restart; account isolation |
| **WP-03 — Event browser** | First convention plugin, durable source observations/guest-event identities, UK event/guest pages, manual refresh and scheduled ingestion, source status | WP-02 | Browse upcoming events and guests with provenance and unknown fields, refresh twice without duplicates |
| **WP-04 — Relevant appearances** | Conservative guest/person matching, watch-based opportunity list, basic admin unresolved identity link/unlink/reject; durable decisions | WP-03 | Watched actor appears in a personalised results page; ambiguous identity is clearly unresolved and can be corrected; resync preserves correction |
| **WP-05 — Second source** | Independent second convention plugin, canonical event/appearance deduplication, cross-source evidence and conflicts | WP-04 | Same real event/guest from two fixtures appears once with two source links; conflicts remain visible; unknown never becomes false |
| **WP-06 — ntfy alerts** | One destination per user, immediate relevant-appearance notifications, idempotent outbox, retries and delivery status UI | WP-05 | Newly matched appearance alerts once; repeat ingest does not resend; failed send retries |
| **WP-07 — Collections** | Generic film/TV collections and signature CRUD; Bond plugin only for 25/27 posters and per-collection unique in-person signer constraint | WP-06 | Generic *Warehouse 13* / *Top Secret!* collection works without Bond; two Bond collectors remain independent; matching guests surfaced |
| **WP-08 — Deployable MVP** | Docker Compose, backups/restores, source/cancellation fixture hardening, security, documentation and end-to-end smoke tests | WP-07 | Fresh install + two-account workflow + restart/restore works; full CI/coverage passes |
| **WP-09+ — Later** | Organiser multi-event templates, extra aggregators, advanced identity review, SMTP/digests, Bond scarcity/optimisation, collection sharing | MVP | Separate vertical slices with their own demonstrations |

**WP order is deliberate:** WP-03 produces a useful convention browser before matching; WP-04 provides personalised discovery before a second source; WP-05 improves reliability without delaying the first usable results; WP-06 adds delivery only once matching is trustworthy. Don't block a usable screen on a future provider or notification system. Use fixtures/demo mode for deterministic walkthroughs, clearly marked as non-live.

**Appearance detail contract (MVP-safe):** preserve optional `attendance_days: list[date] | None`, `autograph_available: bool | None`, `autograph_price: Decimal | None`, and `status: AppearanceStatus | None` per source observation. `None` is unknown; `False` is an explicit negative statement. Source omissions never overwrite known values or imply cancellation. Display source attribution and avoid a false global confidence ranking; defer field-by-field reconciliation and pricing/booking integrations. Add fixtures where one source reports only an appearance and another reports days/autographs, plus explicit cancellation vs disappearance.

**Provider reuse later:** `casttrail-showmasters` and `casttrail-monopoly` can each handle multiple configured event URLs. Shared HTTP/parsing utilities and typed source configuration are useful; generic selector-driven scraping is explicitly deferred until real layouts justify it. Aggregators remain independent plugins. A provider emits observations, never directly mutates canonical data.

**External identities now:** keep `(provider, entity_kind, external_id)` uniqueness, nullable canonical link, provenance and durable manual allow/reject decisions. No automatic name-only confirmed links, candidate scoring engine or full review dashboard. Canonical event deduplication must be conservative, with manual resolution for uncertain cross-source matches.

**Notification scope now:** ntfy only, one enabled destination per user, immediate alerts, retries and idempotency. Keep event/outbox schema sufficiently channel-neutral to add SMTP without changing matching. The richer `notification_channels` and preferences model in §2 is a future extension; implement only required columns/rows in MVP.

### Objective
Implement WP-04 basic identity correction and matching per docs/LLD.md §5 and HLD.md US-05/06/20.

### Constraints
Do not change plugin contracts unless required and documented. No network in tests.
Keep manual corrections authoritative. No commits or pushes without approval.

### Deliverables
- Alembic revision if schema changes
- JSON + HTMX review endpoints
- Identity service and tests
- docs/handoffs/WP-04.md (decisions, tests, remaining issues)

### Acceptance
Rejected candidate never reappears as auto-match; rematch idempotent;
unauthorized user cannot change global identity; `uv run poe check` passes.
```

**Sequencing:** parallelize only after WP-00/01 contracts stabilize; preserve each checkpoint demo. Good parallel tasks: TMDB adapter fixtures and UI templates; notification channel adapters and outbox schema; Bond ranking pure functions and Bond UI mockups. Avoid parallel edits to shared migrations, core schema and plugin API.

## 14. Implementation checklist and definition of done

For each WP: (1) update relevant ADR if design changes; (2) code with typed inputs/outputs; (3) enforce ownership in services; (4) test happy path, ambiguous data, failure and repeat execution; (5) update OpenAPI examples and Jinja2 templates where relevant; (6) run formatting, lint, typing, architecture, tests; (7) record concise handoff and remaining risks. Do not mark a source provider complete based only on a successful scrape of one page.

**Release gates:**
- **MVP usable:** WP-00–08 (with useful intermediate checkpoints after every WP): two users can log in, watch people and productions, discover guests from two sources, fix identities, receive nonduplicate ntfy alerts, and maintain generic or Bond collections.
- **Bond usable:** WP-07: both collectors track separate posters and unique in-person signatures; ranked opportunities are later.
- **Extended:** WP-09+: additional source reliability and opt-in read-only family comparison.

## 15. Traceability and unresolved items

| Concern | HLD reference | LLD implementation section |
|---|---|---|
| Product scope/user stories | §§1–3 | §§0, 13–14 |
| Runtime/plugin architecture | §§4–7 | §§1, 3–4 |
| Identity and discovery | §§8–9 | §§2, 5–6 |
| Bond rules | §10 | §7 |
| UI and JSON API | §§11–12 | §10 |
| Users/notifications/sharing | §13 | §§2, 8–9 |
| Scheduling/operations | §14 | §11 |
| Tests/agent execution | §§15–19 | §§12–14 |

**Open decisions (do not guess silently):** (A) exact FastAPI Users adapter/session strategy supported by pinned release; (B) approved provider access and stable source identifiers; (C) how to verify email destination ownership; (D) whether shared collection view reveals signer names by default; (E) survival and convention-activity evidence sources for Bond scarcity. Address each in a small ADR when implementing the dependent work package.

---

**Maintenance rule:** Update this LLD when an implementation contract changes. Update the HLD only when product scope, user-facing behaviour or architectural intent changes. Keep both documents short enough to be usable as agent context: agents should load the HLD summary plus the relevant LLD section and work package, not paste the entire document into every prompt.
