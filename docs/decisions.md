# Decisions

## WP-0.1: candidate sources (checked 2026-11-08)

Both candidates are aggregators, not convention organisers. Checks were a handful of
polite requests (2 s apart, identifying user agent `CastTrail-spike/0.1`).

### Rostercon (`rostercon.com`)

- **robots.txt:** `User-agent: *` disallows only `/wp-content/uploads/wp-import-export-lite/`.
  A 10 s crawl delay applies to a list of named bots (not us). Sitemaps at `/sitemap.xml`
  and `/sitemap-news.xml`.
- **Terms:** the disclaimer (`/en/legal/disclaimer`) reserves all intellectual property in
  the content to the owner and says copying, disseminating and "any other use" needs
  written permission, except where mandatory law allows it. There is no explicit scraping
  clause and no API or data policy.
- **UK coverage:** good. Event pages carry schema.org `Event` JSON-LD and a stable slug URL.
  Guests link to `/en/people/<slug>`.
- **Decision:** use at personal scale (2-3 users) despite the terms. Low volume, cached,
  at least 10 s between requests, identifying user agent, no republishing of the data.
  The owner has not been asked for permission. This knowingly sits outside the stated
  terms and is the project owner's call. Revisit if the site objects or blocks.

### Comiconomicon (`comiconomicon.com`)

- **robots.txt:** `User-agent: *` allows only `/universe`, `/guest/*`, `/videomedia/*` and
  `/event/*`. Anything else (including `/state/*`) is not allowed, so use event, guest and
  videomedia pages only.
- **Terms:** no terms, privacy, about or contact page was found (all 404).
- **UK coverage:** confirmed but thin in the navigation. The location index lists only US
  states and Canadian provinces, and `/state/<UK name>` is not a real page. UK events do
  appear on guest and videomedia pages. Example: event 42676, "Follow The Yellow Gate
  Road (Take 4)", Lincoln, United Kingdom, 17-18 Apr 2027, with location string,
  coordinates and date in the server-rendered HTML.
- **Open:** how to enumerate UK events without the location pages (via guest pages we
  already track, or `/event/<id>` probing). To be settled in 0.3.

## WP-0.3: identifiers, cancellation signals, external links (checked 2026-11-08)

| | Rostercon | Comiconomicon |
|---|---|---|
| Event ID | URL slug (`/en/event-convention/<slug>`), also the JSON-LD `@id`. Slugs carry the year or edition, so a re-run gets a new slug. | Numeric ID in `/event/<id>/<name>`. The name part is cosmetic. |
| Person ID | Slug (`/en/people/<slug>`). The `-en` suffix is not consistent (`paul-mcgann-en`, `jo-martin`), so match on name plus slug, not slug alone. | Numeric ID in `/guest/<id>/<name>`. |
| Event cancelled | `eventStatus` in the JSON-LD. `https://schema.org/EventCancelled` is confirmed (Star-Con 2026, Hull) and the page also shows "Canceled" with a last-update date. Postponed status not yet seen. | No status field found. A cancelled event simply disappears or stays stale. |
| Guest cancelled | No marker found on the guest grid or in JSON-LD. A withdrawn guest probably just drops from the `performer` list. The page warns the list is subject to change. | No marker found. The page says appearances are "determined by automated algorithms". |
| Organiser link | `organizer.name` in JSON-LD when present. Not always. No organiser URL in the JSON-LD. | `a.eventsitelink` href (for example `mamanoxpresents.com`). |
| TMDB/IMDb link | None on person pages. Matching to TMDB has to be by name. | None on pages fetched. |

**Consequences for the design**
- A guest dropping out of a list is not evidence of cancellation, which matches the project rule that a missing listing is never a cancellation. Treat `EventCancelled` as the only trusted cancellation signal, and only from Rostercon.
- Name-based matching to TMDB is required for both sources, with a manual-identity override.

**Open**
- Where Rostercon shows a cancelled guest (the project owner recalls it does, but could not find an example on 2026-11-08). Not found on the Comic Con Liverpool 2022 or 2026 guest grids, which have no per-guest status. Until one is found, a guest missing from a list is treated as no information. Revisit during WP-3.
- Rostercon began returning HTTP 503 after about 25 requests in 30 minutes. The provider needs a conservative delay and backoff.
- `EventPostponed` or `EventRescheduled` handling: the Wales Comic Con Telford 2021 page failed with 503 before it could be read.

## WP-0.4: sources chosen (2026-11-08)

**Source 1: Rostercon (richer attendance data). Source 2: Comiconomicon.**

- **Gate result: passed, with caveats.** Both sources are parseable from fixtures. Rostercon exposes
  schema.org `Event` JSON-LD (dates, status, venue, performers) that matches its HTML guest list
  exactly, and Comiconomicon exposes events and guests as plain server-rendered HTML.
- **Permission:** neither source grants it explicitly. Rostercon's disclaimer reserves its content
  (see 0.1) and Comiconomicon publishes no terms. Use is personal scale for 2-3 users, which the
  project owner accepted. Ask Rostercon for written permission if the app is ever shared wider.
- **Rostercon is primary** because it has the only trusted cancellation signal (`EventCancelled`) and
  an organiser name. **Comiconomicon is secondary**: organiser links and numeric IDs are good, but its
  guest data is algorithmic and it has no cancellation signal.
- **Overlap exists:** the Lincoln event "Follow The Yellow Gate Road (Take 4)" is on both, which is the
  WP-5 dedupe test pair.
- **Fetching:** no browser needed, plain `httpx2` plus the standard `json` module, with an HTML parser
  added only if a field is missing from the JSON-LD. Rostercon returns 503 under load (about 25 requests
  in 30 minutes), so use a slow rate, a cache, backoff, and never retry in a tight loop.
- **Discovery:** Rostercon has per-type sitemaps (`evenement-sitemap*.xml`) and a search page. Comiconomicon
  has no UK location pages, so find UK events through the guests we track (`/guest/<id>`) and show pages.

## WP-2.1: TMDB provider (2026-11-08)

- **Source of people, films and series:** TMDB v3. Series cast comes from `aggregate_credits` (every season) and films from `credits`. IMDb ids come from the person detail, which is what lets a convention guest link to a person without name matching.
- **Authentication:** the "API Read Access Token" as a bearer header, never the short API key in a query string, so the secret cannot appear in a URL, a log line or a saved fixture. The token is not wired into settings yet; that lands with the search UI (2.3).
- **Parsing is strict:** anything TMDB sends that we cannot read raises `ProviderError("TMDB changed its response format.")` instead of being guessed at. The only leniency is for data that is genuinely often missing: empty strings become `None`, a missing date becomes no year, and a credit with no title is left out.
- **Fixtures** in `tests/fixtures/tmdb/` are trimmed real responses (no biography text, images or crew). `error_not_found.json` is TMDB's documented not-found body, written by hand.
- **Attribution:** TMDB's terms require a visible attribution notice. Add it with the search UI in 2.3, and check TMDB's terms again before caching data for long.

## WP-2.2: catalogue and identity tables (2026-11-08)

Migration 0005 adds `people`, `productions`, `credits` and `external_identities`. Two places differ from the plan's one-line description:

- **`external_identities` is for people only, and has no `kind` column.** The plan had `(provider, kind, external_id)` with a nullable `entity_id`, which would have to point at either a person or a production and so could not be a real foreign key. TMDB numbers films and series separately (the same id can be both), so productions carry their own `(media_type, tmdb_id)` instead. Identities are unique by `(provider, external_id)` and link to a person through `person_id`, which is empty until someone has linked it and goes back to empty (not deleted) if the person is deleted. If IMDb titles are ever needed, add `kind` then.
- **No `normalised_name` column yet.** Name matching is WP-4; adding it then needs a migration anyway and a backfill is trivial on a table this small.

Other choices: a person has at most one credit per production (the writer picks the character), credits and identities cascade or unlink as described in the models, and `productions.media_type` is guarded by a CHECK constraint. `pytest-alembic` does not compare CHECK constraints, so `tests/integration/test_migrations.py` checks the migrated tables' constraints directly.

## WP-2.3: search UI (2026-11-08)

- **One URL, two shapes.** `/search?q=` returns the whole page, and only the results fragment when the request carries `HX-Request`, so search works without JavaScript and HTMX is only an enhancement. The input waits 400 ms after typing and cancels a slower earlier request; checked in a real browser, typing nine characters sent one request. Queries must be 2 to 100 characters. A failed provider call is logged and shown as a calm message with status 200, because HTMX does not swap error responses.
- **Pictures are hot-linked from TMDB's image server**, with `referrerpolicy="no-referrer"` and lazy loading. TMDB therefore sees each viewer's IP address, but not who they are or which page they were on. A missing picture shows the first letter. The image path comes from outside, so it must match `/<letters, digits, _ or ->.<jpg|png|webp>` and the width must be one of `w92`, `w185` or `w342`; anything else gets no picture. If that exposure ever matters, or for the PWA's offline use, add a server-side image cache and serve images from our own origin.
- **TMDB attribution** is on the search page. TMDB's guidelines also ask for its logo; that is not added yet, so check the guidelines before the app is shared more widely.
- **Token:** `CASTTRAIL_TMDB_READ_TOKEN` (the read access token, held as a secret). With it unset, search says it is not set up. `create_app` takes an optional `metadata_provider` so tests can pass a fake.
- Fixed in passing: the navigation overflowed sideways on a phone and now wraps.

## Navigation menu (2026-11-08)

- On screens narrower than 768 px the navigation collapses behind a menu button. It is a progressive enhancement: `nav.js` adds a `js` class and reveals the button, and the CSS only hides the links when that class is there, so without JavaScript the links show and wrap. The button carries `aria-expanded` and `aria-controls`, and Escape closes the menu and returns focus to the button. Checked in a real browser at phone and desktop widths.

## WP-2.4: watches (2026-11-08)

- **One table, two kinds of target.** `watches` points at either a person or a production, enforced by a CHECK that exactly one is set. A user can follow a given person or production only once (two unique constraints). Franchise watches (2.5) are a separate table, so this one stays simple.
- **The details come from TMDB, not from the form.** The Watch buttons post only an id (and the media type for a film or series); the server looks the thing up, so nothing a browser sends reaches the catalogue. This costs one TMDB request per new watch. If TMDB is down or not configured, nothing is added and the person is told.
- **Watching adds the person or production to the shared catalogue** and refreshes its name and picture. A person's TMDB and IMDb ids are recorded as external identities. An identity already linked to a person is never moved, so a link made by hand is never undone by watching.
- **Isolation:** every read and delete takes the user's id. Another user's watch and one that does not exist give the same 404 page. Redirects after a watch go only to `search` or `watchlist`, never to an address from the form.
- **Not done yet:** a film or series watch does nothing until events exist (WP-3) and matching (WP-4). `credits` is still unfilled; the top-billed expansion in WP-6 will fill it.
- **Known:** an admin's navigation now wraps onto a second line just above the 768 px breakpoint. Revisit if more links are added.

## WP-3.1: Rostercon event page parser (2026-11-08)

- **Scope:** `providers/rostercon.py` is a pure parser: HTML in, data out, no network and no database. Fetching, the sitemap and scheduling come in 3.3 and 3.4.
- **Source of truth is the page's `Event` JSON-LD**, read with the standard library only (no bs4). It gives name, dates, venue, city, region, organiser, status and the performer list.
- **`complete` is `True` only when the guest list is whole and understood.** It is `False` when the page has no event, the performer list is missing or the wrong type, or any guest has no name or a link that is not an `https://www.rostercon.com/en/people/<slug>` address. A guest missing from an incomplete listing says nothing about whether they still attend; core must not remove or flag anyone unless `complete` is `True`. An empty performer list is a complete empty listing, but a missing one is not, because that is also how a changed page looks.
- **Status:** only the source's own `EventCancelled` becomes `cancelled`. A missing or unrecognised status is `None`, never cancelled. `EventPostponed` and `EventRescheduled` are mapped to `postponed` from the schema.org names; no real postponed page has been seen yet.
- **Country comes from the location URL** (`/en/location/united-kingdom/...`), because `addressCountry` is written in French ("Royaume-Uni"). Callers should compare against the slug `united-kingdom`.
- **Links from outside are checked** (https, Rostercon's own host, the expected path, a single slug) before they become a guest key or a stored address.
- **Not done:** no `attendance_days` or autograph fields, because Rostercon gives none. No cross-check of the JSON-LD against the visible guest tiles; the saved fixtures hold only the JSON-LD, so that check could not be tested. Add it when real pages are fetched.

## More sources checked (2026-11-08)

- **UK Conventions listing** (`sites.google.com/site/ukconventions/`, by one person, copyright Caz Rudd): a hand-written list of UK events by month, from November 2026 to December 2027. Each entry has a date line, the event name linked to the organiser's own website, a venue line and sometimes a description. It has **no guest names, no cancelled or postponed markers and no stable ids**, dates carry no year (it comes from the page), some venues lack a town, and there are typos. Google's robots.txt allows the pages, but the footer reserves copyright on the text and the owner takes listing requests by email. **Verdict: usable only as an optional discovery list** (event names and organiser links), about 14 requests for the year ahead. Deferred, because Rostercon's sitemap already covers discovery and the plan needs only two sources.
- **FanCons** (`fancons.co.uk`): every request, including `robots.txt`, gets a Cloudflare managed challenge (HTTP 403, "Just a moment..."). That is a deliberate block on automated clients, so it is **not usable**, and it is not to be worked around with a browser. It is described elsewhere as a database of UK convention guests, so it may be worth asking the owner for permission or a data feed. I have not seen its data.
- **Source order stays:** Rostercon primary (parser built in 3.1), Comiconomicon secondary for guests, then UK Conventions for discovery if ever needed.

## WP-3.2: discovery tables (2026-11-08)

Migration 0007 adds `conventions`, `convention_sources`, `source_observations`, `appearances` and `appearance_sources`. `job_runs` is left to 3.4, where it is used.

- **Observations store a hash, not the page or its data.** The plan had a bounded `payload_json`. Our parser's output is small and saved fixtures cover debugging, so a hash (to see that nothing changed) plus whether the listing was `complete` is enough. Rows are pruned to the last 3 per `(provider, source_key)` in 3.3.
- **A convention holds the latest details; sources are separate rows.** `convention_sources` is unique by `(provider, source_key)`, so one source key maps to exactly one convention. Two sources listing the same event become two convention rows until WP-5 merges them; no merge table exists yet.
- **Guests are `external_identities` first.** An appearance is `(convention, external_identity)`, unique, with `person_id` empty until matching (WP-4) links it. Deleting a person unmatches the appearance and keeps it; deleting an identity removes its appearances.
- **"No longer listed" is a flag, not a status.** `appearance_sources.listed` goes false only when a complete listing leaves the guest out. `status` is the source's own word for the guest and is empty unless it gave one. Both `conventions.status` and `appearance_sources.status` are limited to `scheduled`, `cancelled` and `postponed` by a CHECK, which `pytest-alembic` does not compare, so the migration tests check it directly.
- **Not enforced:** end date not before start date. Dates come from outside, and a bad one should be stored as missing by the discovery service rather than make a whole sync fail.

## Running behind HTTPS on a tailnet (2026-11-08)

Found while putting a real instance on Tailscale. Both matter again for WP-8.

- **Asset links are paths, not full addresses.** Starlette's `url_for` builds `scheme://host/path` from the request. Behind an HTTPS proxy the app sees plain HTTP, so every stylesheet, script and icon became an `http://` address and the browser blocked them all as mixed content. Templates now get a `url_for` that returns only the path, which resolves against whatever address the browser used and needs no proxy configuration. A test sweeps every page for absolute asset links.
- **Sign-in over plain HTTP failed from one device.** A plain-text POST arrived but a form with a password field was reset before reaching the server. The cause was on that device or its network, not in the app. HTTPS fixed it.
- **Granian closes idle HTTP/1 connections after about 30 seconds** and the longest header-read timeout it allows is 60 s. A browser that reuses the connection for a form POST after that gets `ERR_CONNECTION_RESET`, and the POST cannot be retried. Running Granian with `--no-http1-keep-alive` avoids it. `uv run poe serve` does not set that flag yet.
- **Tailscale HTTPS recipe:** enable HTTPS certificates in the Tailscale admin console, run the app on a loopback port with `CASTTRAIL_COOKIE_SECURE=always`, then `tailscale serve --bg --https=443 http://127.0.0.1:<port>`. This is tailnet-only, not public. The app then sees every request as coming from 127.0.0.1, so the per-address sign-in limit is shared by everyone, which is the trusted-proxy caveat already noted for WP-8.

## WP-3.3: discovery service (2026-11-08)

`DiscoveryService.ingest(provider, convention, listing)` records one parsed page. It is all or nothing: one commit, and a rollback on any failure. Migration 0008 adds `external_identities.display_name`, because an unmatched guest otherwise had no name to show on the 3.5 pages.

- **Idempotent.** Ingesting the same page twice creates nothing new; only `last_seen_at` moves. The convention's `updated_at` changes only when a detail actually changed.
- **A detail the source leaves out never clears what is known.** Only a stated value replaces a stored one, so a page that stops saying "scheduled" does not erase it, and only a stated `cancelled` cancels an event. A source can also take a cancellation back by stating `scheduled`.
- **A guest missing from a listing is never cancelled.** Only a **complete** listing marks a guest `listed = false`, and only for that provider's own source line. An incomplete listing, including an empty one from a page that changed shape, unlists nobody. A guest who returns is listed again with no duplicate.
- **Hand-made links are never touched.** The service sets an identity's `display_name` but never its `person_id`, nor an appearance's.
- **Observations** hold a content hash (guest order and `etag` ignored) plus `complete`, and are pruned to the last 3 per page. `IngestResult.changed` says whether the hash differs from the previous observation, which the source health page (3.6) can use.
- **Not done:** the `identifiers` on a guest (such as TMDB or IMDb ids) are not turned into extra identities yet; Rostercon gives none. Field lengths are not clipped, which SQLite does not need. Fetching and scheduling are 3.4.

## WP-3.4a: Rostercon lists and finding a person (2026-11-08)

**Decision: refresh what people watch, not everything.** A full sweep of UK events was measured and set aside for now: the UK list is about 160 pages of 15, and Rostercon sends no `ETag` or `Last-Modified`, so unchanged pages cannot be skipped. Fetching only what is watched is far cheaper (one person page plus one page per upcoming event, about 10 to 40 requests a day for a handful of watches). The 3.5 event browser will show whatever has been collected, and a full sweep can be added later if wanted. Watched films and series are not looked up on Rostercon directly (its series sitemaps number 47 files); WP-6 expands them into their top-billed cast, which goes through the person route.

- **A person's page lists their events.** The person page carries a teaser of recent and upcoming events and a link to the full list at `/en/people/<slug>/events/<id>`, newest first. For one real person the teaser held every upcoming event, but the cap is unknown, so 3.4c should also read the first page of the full list.
- **Name search does not find people.** Rostercon's own search (`/en/?s=`) returns news articles, not people. The people sitemaps (`acteurs-sitemap*.xml`, 7 files of about 1,000 people each, about 186 KB per file) map every English person page, so a name is matched to a page locally against a cached index. 3.4b caches the sitemap, so these are about 8 requests every few weeks.
- **Matching is deliberately strict.** A TMDB name becomes a slug (accents folded, punctuation to hyphens) and matches a page only if the page's address is that slug, optionally followed by `-<number>` and `-en`. Several matches means the name is ambiguous and the caller must not pick one; a name whose slug differs (such as "50 Cent" against `50-cent-curtis-jackson-en`) finds nothing rather than guessing. Resolving these properly is WP-4's job.
- **Row parser:** the same `<ul class="list-inline row">` markup serves a person's page, a show's page and the country list, and gives name, date, place and show. A row that cannot be read is counted in `skipped` so a changed page shows as a rising number. Names and addresses from outside are only accepted if they point at `https://www.rostercon.com/...`.
- **Sitemaps are read with a pattern, not an XML parser,** so a hostile file cannot use entity expansion. A test feeds one.
- **Correction:** I earlier said the UK list runs newest first so a sweep could stop at today's date. It does not: its first row is out of place (Jul 2027 before Oct 2027). A person's own events list is strictly ordered. Do not rely on the country list's order.

## WP-3.4b: polite Rostercon fetching (2026-11-08)

- **Where:** `providers/rostercon_client.py`. It only fetches; parsing stays in `rostercon.py` and nothing touches the database.
- **Spacing:** at least 10 s between requests, enforced under a lock so concurrent callers still queue. Rostercon answers 503 after about 25 requests in 30 minutes, and 10 s keeps a refresh well under that for a handful of watches. A failed request counts for spacing too.
- **Backing off:** after a 429 or 503 nothing is sent until a cooldown ends: the longer of `Retry-After` (seconds only, a date is ignored) and 10 s doubled for each refusal in a row (20, 40, 80 s and so on), capped at an hour. Calls during the cooldown raise at once without touching the network, with the time left in `retry_after`. Any success resets the doubling. There is no automatic retry: the caller decides, and the refresh job simply stops and tries on its next run.
- **Only Rostercon:** the address must be `https://www.rostercon.com/...` with no user part, or no request is made. Redirects are not followed, so a 3xx is an error; if Rostercon starts redirecting, this will show up as a rising error rather than a request sent somewhere unchecked.
- **Limits:** a page over 5 MB is refused. The body is streamed and counted as it arrives (on the decoded bytes), so a huge `Content-Length`, an endless body or a compression bomb is stopped at the limit instead of being read in full first. Error messages carry no address and no library text.
- **People index:** `PeopleIndex` reads `acteurs-sitemap.xml`, then `acteurs-sitemap2.xml` upward until one is missing (at most 20), and keeps the result in memory for 14 days. A refusal part way keeps the files already read, so the next call carries on instead of starting over. A file with no people is an error and is not cached. The cache is per process: a restart costs about 8 requests (roughly 80 s at the spacing above).
- **Not done:** the index is not stored in the database. If restarts become frequent, persist it.

## WP-3.4c: refreshing what people watch (2026-11-08)

- **Where:** `discovery/refresh.py` (`RosterconRefresh`), table `job_runs` (migration 0009), model `JobRun`. There is no scheduler yet; 3.4d calls `run()`.
- **What one run does:** take every distinct person anyone watches; match each name to exactly one Rostercon page; fetch the person page and, if it links one, their full list of events; collect every listed event once (two watched people at one convention fetch it once); fetch the pages of events that start no earlier than two days ago, soonest first, at most 60 per run; ingest each through `DiscoveryService`. Nothing watched means no request at all, not even the sitemap.
- **Never guessed:** a name with no page, or with several, is counted (`people_unmatched`, `people_ambiguous`) and left alone. The guests it stores are not linked to people here; that is WP-4.
- **The lock** is the database: a partial unique index allows one `running` row per job, so a second refresh, even from another process, fails to insert and returns `None` without touching Rostercon. A `running` row older than 2 hours is taken to be a dead run, marked failed, and replaced. Finished runs are never altered.
- **Failure handling:** a refusal (429 or 503) stops the run at once and records it as failed with the reason, keeping everything stored so far; the next run carries on, and the fetcher's cooldown applies in between. A person or event that cannot be fetched or read is counted and skipped, and the run still succeeds. A crash is recorded as failed, the lock is released and the error is raised.
- **The run's record** is a JSON count of what happened (`RefreshReport`) kept in `job_runs.summary`: no page text, URLs or credentials. 3.6 will show it.
- **Gate check:** a second refresh makes the same requests and changes nothing (no new conventions or appearances, `events_changed` 0).
- **Not done:** an event's own date range is not read until its page is fetched, so an event that started more than 2 days ago but is still running (rare) is missed. Rostercon sends no `ETag`, so every upcoming event page is refetched each run; at 10 s spacing the cap keeps a run near 10 minutes.

## WP-3.4d: the daily timer and the refresh button (2026-11-08)

- **Timer:** APScheduler 3.x (`AsyncIOScheduler`, pinned `<4` as the plan says) runs inside the app. It is **off by default**: set `CASTTRAIL_REFRESH_ENABLED=true` to run the refresh daily at `CASTTRAIL_REFRESH_HOUR` (0 to 23, server local time, default 4). Off by default so a fresh install makes no outbound requests until you choose to. One run at a time, and a start missed by up to an hour (restart, busy server) still runs once.
- **Button:** `/admin/refresh` (administrators only; others get 404 like the other admin pages) lists the last ten runs from `job_runs` and has a "Refresh now" button. The refresh takes minutes at Rostercon's pace, so the button starts it in the background and redirects at once with a message; a second press while this process is still running one says so and starts nothing. The database lock in 3.4c covers any other process.
- **Failures never escape:** a timer or button run that crashes is logged and ends there; it cannot stop the scheduler or the app. A run that Rostercon cut short is logged as a warning and shown in the table with its reason.
- **Shutdown:** the timer is stopped, a running refresh is cancelled, and the Rostercon HTTP client is closed. A cancelled run leaves a `running` row that the next run replaces after 2 hours (the stale rule from 3.4c).
- **Wiring:** `create_app(refresher=...)` takes a stand-in for tests; otherwise one `RefreshJob` is built with a single fetcher and people index kept for the life of the app, so Rostercon spacing and the cached index carry across runs. Creating the client makes no network request.
- **Not done:** no per-user view of the data yet (3.5), and the table shows only counts, not which people were unmatched (3.6 and WP-4).
