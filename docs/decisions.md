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

## WP-3.5: events and guests pages (2026-11-08)

- **Pages:** `/events` lists conventions that have not finished (a 2-day grace for ones still running), soonest first, 50 a page. `/events/<id>` shows one convention and its guests. Both need a sign-in; an unknown id is a 404. The data is shared by everyone, so there is no per-user filtering (nothing here is personal).
- **UK by default:** the list shows conventions Rostercon places in the United Kingdom, with a link to show all. A convention with no known country appears only under "all", so a missing country is never assumed to be the UK.
- **Unknowns are shown as unknown:** a missing date, place, status or organiser says "unknown" instead of being left blank or guessed. Events with no date come last, never hidden.
- **Guests:** shown by the source's name with a link to its page. "Confirmed" means the source stated scheduled, "Cancelled" and "Postponed" are only what the source stated, "Listed" means listed with no status, and "No longer listed" (listed first, then these last) is explicitly not a cancellation, with a note on the page. Guests are not yet linked to your watched people; that is WP-4.
- **Not done:** no filter by date or watched person, and no highlighting of watched people until WP-4 links them.

## WP-3.6: source health (2026-11-08)

- **Page:** `/admin/health` (administrators only, 404 for others) summarises the Rostercon refresh from what is already recorded: conventions stored, the last page read, the last finished refresh and its result, the last success, and failed refreshes in a row. A run still in progress is not judged.
- **Warnings are plain words:** the last run stopped early (with the reason), some event pages could not be read ("Rostercon may have changed its layout. Stored data was kept."), some pages could not be fetched, some watched people have no single Rostercon page, no refresh has succeeded, or the last success was over 3 days ago. They mean "look at this".
- **A parse failure keeps old data:** an event page that no longer parses is counted and skipped; nothing is stored, changed or unlisted from it. A test refreshes, then serves unreadable pages, and checks that conventions, guests and their listed flags are unchanged.
- **Not done:** only Rostercon is covered; Comiconomicon has no refresh yet. There is no push alert for a broken source (WP-6 notifications could add one).

## WP-4.1: identity resolver (2026-11-09)

- **Decision (user):** an exact name match with a watched person is only ever **Possible** and needs a person to confirm it. Only confirmed matches will drive alerts.
- **Where:** `matching/resolver.py`, pure functions with no database. `resolve(guest, candidates)` returns a `Resolution` (kind, person, reason). Later chunks feed it stored decisions and persist the result.
- **Order of evidence:** a link made by hand (`manual`), then an id both sides share (`shared_id`, such as the same TMDB id), then an exact name match (`possible`), otherwise `unresolved`. A person the guest was rejected as is removed before any of this, even for an id match, so a rejection sticks. A manual link wins over a rejection.
- **Names:** compared after folding accents, case and punctuation, as whole names. "Paul" does not match "Paul McGann". Two watched people with one name are not guessed at: the guest stays unresolved with the reason. Rostercon gives no ids today, so in practice matching is by name until a second source arrives.

## WP-4.2: identity decisions (2026-11-09)

- **Table:** `identity_decisions` (migration 0010): one row per (guest, person) saying `confirmed` or `rejected`, who decided and when. A partial unique index allows at most one confirmed person per guest. Deleting a guest or person removes its decisions; deleting the user keeps them with no name on them.
- **Service:** `identity/decisions.py` (`DecisionService`). Confirm links the guest (`ExternalIdentity.person_id`) and every one of its appearances to the person, replacing any earlier link and overturning a rejection of the same person. Reject records it and, if that person was the link, removes the link. Unlink removes the link without rejecting anyone. Forgetting a rejection lets the person be offered again. `state()` returns what the resolver needs (the linked person and the rejected set).
- **Shared decisions:** a decision is about the guest, not about one user, so it applies to everyone. With 2 or 3 trusted users that is intended; it means one person's confirmation is seen by the others.
- **Sync never undoes them:** three re-ingests, including a renamed guest, leave decisions and links as they were. Found while testing: a *new* appearance for an already-linked guest started unlinked, so ingest now copies the guest's link onto it. That still never overrides a decision, because only the link made by hand is copied.

## WP-4.3: matches (2026-11-09)

- **Table:** `matches` (migration 0011): one row per user, guest appearance and watched person, with `kind` (`confirmed` or `possible`), a reason shown to the user, and a never-empty `match_key` (`person:<id>`). `(user_id, appearance_id, match_key)` is unique, so a later production or franchise watch can add its own keys without the nullable `watch_id` breaking uniqueness. Deleting the user, appearance or person removes the match; unwatching empties `watch_id` until the next rematch removes the match.
- **Service:** `matching/service.py`. `MatchService.rematch()` recomputes every match from the current guests, watches and decisions and brings the table into line: it adds, changes (kind, reason) and removes, all or nothing. It only reads decisions, never writes them. Running it twice changes nothing, and `updated_at` moves only on a real change.
- **Rules applied:** only guests with a source still listing them count, so a guest who drops off loses the match and gets it back if relisted. A guest linked by hand is `confirmed` for users who watch that person and invisible to others. Exact-name matches are `possible`. A rejection hides the guest from every user. Two watched namesakes are not guessed.
- **Not done:** nothing calls `rematch` yet. 4.4 shows the matches and later chunks run it after each refresh; the refresh job does not run it yet.

## WP-4.4: my relevant appearances (2026-11-09)

- **Page:** `/appearances` ("My appearances" in the nav) lists where the signed-in user's watched people will be, soonest first, from `matches` filtered by that user. Finished events are left out (2-day grace); undated ones come last; confirmed sorts before possible on the same day. Each row says why it matched, and a cancelled or postponed convention is flagged.
- **Labels:** "Confirmed" means a person linked the guest; "Possible" means only the name matched. The page says so, because a namesake is possible. No alerts depend on Possible.
- **Matches stay current:** `rematch` runs after every refresh (even one cut short, since it still stored pages), when a person is watched, and when a watch is removed. The refresh record now counts matches added and removed.
- **Not done:** there is no screen yet to confirm or reject (4.5), so everything starts as Possible.

## WP-4.5: review guests (2026-11-09)

- **Page:** `/admin/review` (administrators only; 404 for others, and a cross-site post is refused like every other post). It has three parts: guests waiting to be confirmed (name matches, with how many events each is at), linked guests, and a search to link any convention guest to any person.
- **Actions:** Confirm, Reject, Unlink, and "Confirm all exact-name matches". Every action runs through `DecisionService` and then rematches, so the user pages update at once. Unknown guests or people are a 404 and change nothing.
- **Bulk confirm is cautious:** it confirms only guests with exactly one suggestion. A guest suggested for two different people (two users each watching a different person of that name) is left for a person to decide. Each confirmation is saved on its own, so a failure part way leaves the earlier ones in place.
- **Search:** by part of the name, taken literally (`%`, `_` and `\` are escaped), cut to 100 characters and to 20 results. TMDB identities are never shown as guests. Only the first 500 people are offered to link to, which is plenty for 2 or 3 users.
- **Decisions are shared:** as decided in 4.2, one confirmation applies to everyone, so this is an admin screen and not a per-user one.

## WP-4.6: corrections survive re-syncs, and the WP-4 gate (2026-11-09)

- **Test:** one test watches two people, refreshes, confirms one guest and rejects another, then refreshes and rematches three more times. Decisions, links, match kinds, reasons and creation times are identical afterwards, no match is added or removed, and no convention is duplicated. Breaking the sync (clearing a link, dropping decisions) or the rematch (ignoring rejections) makes it fail.
- **Gate:** `poe coverage-critical` now holds `identity/` and `matching/` to 95% as well as `auth/`.

## WP-5.1: Comiconomicon parser (2026-11-09)

- **Scope:** `providers/comiconomicon.py` is a pure parser (HTML in, data out), built with the standard library's `html.parser` like the Rostercon one. Pages used are the ones its robots.txt allows: `/event/<id>`, `/guest/<id>` and `/videomedia/<id>`. Fixtures: two event pages, one film page (Warehouse 13) and one guest page (Andrew Jackson, fetched once on 2026-11-09 with an honest user agent).
- **Why it matters (user note):** Comiconomicon organises appearances by film, series and guest better than Rostercon. A film or series page lists the events whose guests were in it, and a guest page lists their events and every film and series they were in (`Role`). That is the route for watched productions in WP-6; Rostercon stays the route for watched people.
- **Event page:** `parse_event_page` gives a `ConventionInfo` and the guests. The event id is the source key. The location is one free-text line with no venue/city split, so it is kept whole as the venue; the country is set to `united-kingdom` (and a region, for England, Scotland, Wales or Northern Ireland) only when the line **ends** with one of those names, otherwise it is left unknown. No organiser name is given (only a link), so it is left unknown; no status exists on the site, so a cancellation is never inferred.
- **Never complete:** the site says appearances come from automated matching and may change, so the guest list is returned with `complete=False`. A guest missing from a later fetch therefore never becomes "no longer listed" from this source.
- **Lists:** `parse_event_list` reads events from a film, series or guest page, and `parse_guest_page` adds the roles. Dates such as `17 - 18 Apr, 2027`, `13 Nov, 2026` and `30 Dec - 2 Jan, 2027` are understood (a start in the previous year is inferred only for a month that wraps); anything else gives unknown dates.
- **Untrusted input:** every link must match `/event|guest|videomedia/<digits>[/safe-name]` and its number must equal the element's `data-id`; addresses are rebuilt as `https://comiconomicon.com/...`, never copied. Markup in names stays text. Garbage input never raises.
- **Not done:** no fetching (that is the same polite fetcher pattern, a later chunk) and no dedupe against Rostercon events (5.2).

## WP-5.2: strict event dedupe (2026-11-09)

- **Rule:** when a source lists an event we have not seen from that source, it is compared with conventions that source has not listed (`discovery/dedupe.py`, pure). **Same** requires: the same name once tidied (case, accents, punctuation, "&" read as "and"), the same start day, and end days that agree or are not both known. Exactly one such convention is joined: the new source is attached to it and no second convention is created. It works whichever source arrives first.
- **Maybe, for a person:** anything weaker that could still be one event is not merged, only recorded in the new `convention_merges` table (migration 0012): the same name with a different end or start day, the same name with an unknown day, alike names (one name's words inside the other's, or half the words shared) on overlapping days, and two or more exact matches (never guessed between). Each pair is stored once with a reason. A pair already decided, such as one rejected, is not suggested again. Different days or unalike names are simply different events.
- **No merging within one source:** two events from the same provider are never joined, so a recurring event with a new key stays separate.
- **Whose details win:** `_PRIORITY` ranks sources (Rostercon, then Comiconomicon). The best-ranked source listing a convention may change its name and details; any other only fills in what is still empty. This stops two sources fighting over a name or a date on every refresh, and a better source arriving later takes over. Per-source values and the display of conflicts are 5.3.
- **Guests are untouched:** a merged convention now holds appearances from both sources as separate guests; recognising the same person across sources is 5.3.
- **Not done:** the screen to decide suggested pairs, and the join itself for them, are 5.4. With an event whose start day is unknown, every convention the source has not listed is considered; that is fine for 2 or 3 users and would need a narrower query at scale.

## WP-5.3: guests and disagreements across sources (2026-11-09)

- **Each source's own view is kept** (migration 0013): `convention_sources` now records the name, start, end and venue that source itself gave (`seen_*`), updated on every ingest, separate from the merged convention. The merged convention still follows the 5.2 lead-source rule. Rows from before this chunk show "Unknown" until the next refresh.
- **Event page:** a "Sources" table shows what each source says. If they state different names (compared tidied), start days or end days, a notice names the fields. A fact only one source states is never a conflict, and the free-text venue is shown but not compared (the sources describe places differently).
- **One guest, many sources:** a person listed by both sources shows once, with each source named. A listing counts as the same guest if it is linked to the same person or, failing that, has the same name at that event (case, accents and punctuation ignored); an unlinked listing also joins a linked one of the same name. This is the single rule behind both the guest table and the guest count on the events list, so they cannot disagree.
- **Statuses are only what sources said:** the overall status is cancelled if any source says so, otherwise postponed, confirmed, listed, or no longer listed when nobody lists them. If one source says cancelled and another still lists the guest, the row says **"Sources disagree"**; nothing is resolved for the reader. Comiconomicon never marks a guest as no longer listed.
- **My appearances:** a watched person at a merged event had a match per source; the page now shows each person once per event, taking the surest (confirmed over possible).
- **Found while testing:** an interrupted mutation run had left a mutant (`if False`) in the grouping code. It was caught because a new test for joining an unlinked listing failed. Every mutation run now restores files from a saved copy and the tree is checked afterwards.
- **Not done:** per-source *alerts* (WP-6) will need one alert per person and event, not per source; that rule is noted here for then. The merge of suggested pairs by a person is 5.4, which is where real conflicts will mostly appear.

## Robust startup, automatic updates and backups (2026-11-09)

Requested by the project owner: start-up should update the database by itself, never crash, and the data should be backed up. See `docs/operations.md` for how to run and restore.

- **Automatic update:** at startup the database is brought up to date (`CASTTRAIL_AUTO_MIGRATE`, on by default). Alembic is run in a worker thread; whether anything is pending is read straight from the file, so a start with nothing to do costs a single query.
- **Copy first, always:** if an update is pending and the database already holds data, a backup is taken first, and **no update is attempted if that backup fails**. A failed update leaves that copy as the way back.
- **Backups (`maintenance.py`):** SQLite's online backup (consistent while the app is writing), written under a temporary name and renamed when complete, so a crash never leaves something that looks like a backup. Kept in a `backups` folder beside the database (or `CASTTRAIL_BACKUP_DIR`). Daily copies are pruned to the newest 14; copies taken by hand, at startup or before an update are never deleted automatically. Timers: daily at 03:30; a catch-up copy at startup if the newest is over a day old (a PC that sleeps overnight); "Back up now" on the admin Source health page.
- **Never crash:** every startup step is wrapped. A *problem* (folder cannot be made, check or update or safety copy fails) means the app starts but every page, apart from static files, answers a plain 503 page and `/health` says `degraded`; no timers start and no admin is created. A *warning* (a catch-up backup, the timers or the first admin failed) leaves the app working and is shown on the Source health page. Invalid settings give an app that only reports which settings are wrong, by name and never by value. Details go to the log only; the page and the health answer carry no paths, errors or secrets.
- **Why 503 and not a half-working app:** with an unknown database state, serving pages could write to a broken database. A clear stop with the reason is safer.
- **Tests:** the suite never reads a developer's `.env`, and builds its settings with backups off unless a test is about them.
- **Also:** `uv run poe serve` now runs with `--no-http1-keep-alive`, the fix for the idle form resets seen over Tailscale.
- **Not done:** restoring from the admin page (restore is a documented manual step, since replacing the live database from inside the running app is risky); copying backups off the machine (do that by hand for now).

## Docker (2026-11-09)

The app is meant to run in Docker. Files: `Dockerfile`, `compose.yaml`, `.dockerignore`, `scripts/healthcheck.py`; how to run, update, move an existing database in and restore is in `docs/operations.md`.

- **Follows Menagerist's pattern** (the owner's other project): multi-stage build with pinned base digests, uv-managed Python, a distroless non-root runtime, read-only root, all capabilities dropped, `no-new-privileges`, a 512 MB memory limit, log rotation, `restart: unless-stopped`. No Postgres or Node, so it is one container.
- **One worker, one container:** the app keeps its timers and Rostercon request spacing in memory, so more than one copy would send more requests than intended.
- **Data:** the database lives in a *named volume*, not a bind mount, because SQLite's write-ahead log is not reliable on a bind mount from a Windows or macOS host. Backups go to a plain `./backups` folder so they are easy to see, copy and sync.
- **Port:** published only on `127.0.0.1:8002`, which is where `tailscale serve` already points, so Tailscale and HTTPS carry over unchanged.
- **Settings:** the same `.env` is passed in by compose (never baked into the image: it is in `.dockerignore`). The image sets the database path, backup folder and migrations folder.
- **Migrations folder is configurable** (`CASTTRAIL_MIGRATIONS_DIR`): the code used to find `migrations/` beside the source tree, which does not hold for an installed package. The image copies it to `/app/migrations`.
- **Healthcheck** answers healthy only on a 200 from `/health`, so a degraded app (see startup notes above) shows as unhealthy with the reason in `docker logs`.
- **Tested for real, not just built:** a throwaway container with a fresh volume migrated itself, bootstrapped the admin from settings, served the login, dashboard and events pages, wrote a startup backup into the mounted folder, kept its data across a restart and reported healthy. With no backups folder mounted it still served pages (the backup became a warning), which exposed that a failed cleanup could hide the real error; fixed.
- **Poe tasks:** `docker-up`, `docker-build`, `docker-down`, `docker-logs`, `docker-config`. The version comes from the `calver-scm` CLI in the locked `release` dependency group (`uv run --locked --only-group release calver-scm --format plain version`, as Menagerist does), worked out on the host and passed in as a build argument, because the image has no git history; the built image reports it.
- **Not done:** building and publishing the image in CI; restoring from the admin page.

## Logo: a wavy trail tucked behind the star, and a generator for the icons (2026-11-09)

- **Fix:** the trail used to end beside the star with its round end poking out past the leg's edge. It is now a wavy trail that rises from the bottom-left and runs *behind* the star, its end hidden inside the star's body. Several waves were rendered side by side and chosen by eye (a gentle S, a slight wave, a full wave); the slight wave was chosen.
- **Pinned by tests, from the SVG itself:** the trail is drawn before the star so the star covers it; its rounded end lies entirely inside the star's outline (all round the rim, not just the centre), so no white edge can show beside the star; it starts below and to the left of the star; and no white shows in the rendered icon above the star's lower tips. The visible part of the trail is checked to be solid white along its whole length.
- **Generator (Menagerist's approach):** `scripts/generate_icons.py` (`poe generate-icons`) renders `icon-192`, `icon-512`, `icon-maskable-512`, `apple-touch-icon` and `favicon-32` from `logo.svg`, and writes `logo-maskable.svg`, so nothing is hand-edited. The PNGs were committed in #22 with no generator, so they could not be reproduced. The generated files stay committed; a build does not need Pillow.
- **Only what it can draw:** a rounded tile, a stroked curve (`M`, `C`) and a filled polygon (`M`, `L`, `Z`). Anything else is an error, so a new shape cannot be silently left out.
- **Drift test:** a test re-renders every icon from `logo.svg` and compares pixels with the committed files, so changing the logo without regenerating fails the suite. Pillow is a dev dependency for this.
- **Fixed while building it:** drawing a curve as joined line segments left faint hairline gaps in the stroke. It is now drawn with a dot at every point, and a test checks every point along the trail is solid.
- **Formats kept:** the maskable and Apple icons are full-bleed RGB, the others RGBA with transparent corners, as the existing tests require; the maskable mark sits inside the central 64%.

## WP-5.4: merging events a person has judged the same (2026-11-09)

- **Screen:** `/admin/merges` (administrators only; 404 for others; cross-site posts refused) lists pairs 5.2 could not decide, side by side: name, dates, place, status, which sources list each, and how many guests, with the reason it was suggested. For each pair: **keep one** (merge) or **different events** (reject). Rejected pairs are listed below with **Suggest again**.
- **What a merge does (`discovery/merging.py`):** the chosen event is kept. The other's sources move onto it, anything the kept event does not know (dates, place, organiser, status) is filled in from it, its guests are folded in, and it is deleted. A guest that is the same identity at both becomes one row with both sources' lines (the kept side's own line wins), and a person link made on the dropped side carries over unless the kept side already has one. All of it is one transaction: a failure part way changes nothing. Matches are recomputed afterwards.
- **Today's guests:** the two sources make their own identities, so the same person listed by both stays two guest rows after a merge and is shown as one by the display rule from 5.3. Folding by identity matters only once identities are shared across sources; it is there so the unique rule cannot crash a merge, and is tested with that shape built by hand.
- **Never two events of one source:** if one source lists both events the merge is refused with a reason and nothing changes. A source's two events are two events.
- **Only the pair's own events:** choosing an event that is not in the pair is a 404. Mutation checking found this guard was not actually tested; without it, a stranger could have been chosen as "the kept one" and the pair's first event merged into it.
- **Other pairs follow:** the dropped event's other suggested or rejected pairs are moved onto the kept event. If the kept event already has a pair with the same other event, the two become one, and a rejection wins over a suggestion.
- **Later refreshes:** the dropped event's source keys now point at the kept event, so a refresh updates it and does not recreate the duplicate or suggest it again. A rejected pair is never suggested again unless someone chooses "Suggest again".
- **Cannot be undone:** the dropped event is deleted, and its pair rows with it, so no "merged" row survives (the status is allowed but never stored). The page says so before the buttons.
- **Gate met:** the same event from both fixtures shows once with two sources, joined automatically when strict and by a person otherwise.
- **Still missing for WP-5 to be useful live:** nothing fetches Comiconomicon yet (parser and dedupe only), and finding the Comiconomicon page for a TMDB person or film is an open question, because its robots.txt allows no name search. Added to the plan as 5.5.

- **Time in tests (project owner's direction):** tests control time with the `time-machine` library (dev dependency; its pytest fixture), not with helper clock classes or an injected `now`. `MergeService` therefore has no `now` parameter and uses `utc_now()`. Older code still has injectable clocks (auth, discovery, matching, refresh, the sign-in throttle); moving them over is a separate cleanup, not done here.

## WP-5.5a: using Comiconomicon's search (2026-11-09)

- **Decision (project owner):** use `https://comiconomicon.com/xhr/indexautocomplete.php?term=<name>` to turn a name into the site's ids. It is the drop-down behind the site's own search box. The robots.txt lists `Allow` for `/universe`, `/guest/*`, `/videomedia/*` and `/event/*` and has no `Disallow`, so read literally nothing forbids it; read by intent it may mean "only these". There is no terms page and no contact address to ask. The owner chose to use it; it sits outside the paths the site lists, is used at personal scale, and should stop if the site objects or blocks. Recorded so it can be revisited.
- **How it was found to behave (4 single requests, on 2026-11-09, with an honest user agent):** a JSON array. Each entry has `id`, `name`, `type` (`guest`, `videomedia` or `event`), `data1` to `data3`, `appearances` (upcoming count), `new`, `url` and an `html` snippet for the drop-down. For a film `data1`/`data2` are the year; for an event they are the start and end day and `data3` is the place; a guest has none. Fixtures: one answer of each kind in `tests/fixtures/comiconomicon/autocomplete_*.json`.
- **Parser (`parse_autocomplete`):** strict, like the page parsers. An entry is used only if its type is known, its name is not empty, and its address is that type's own path with the same number as its `id` (the address is rebuilt, never copied). Years must be four plain ASCII digits and days real ISO dates, or they are unknown. The `html` field is ignored. Entries are kept once, at most 100 are read, and unusable ones are counted. A body that is not a JSON array is a `ProviderError`, so a changed answer is noticed and not read as "no results".
- **Matching a name (`match_by_name`):** the whole name must be equal, ignoring case, accents and punctuation ("&" as "and"), and for a film or series the year must agree when it is known. None means not found, one is the answer, several mean ambiguous and the caller must not guess, the same rule as for Rostercon's people.
- **Not done in this chunk:** nothing calls the search yet. 5.5b adds the polite fetcher and caches each answer (one lookup per newly watched name, at least 10 s apart, with backoff); 5.5c refreshes from the guest pages found; 5.5d does watched films and series through their film pages.

## "Database is locked": no transaction may stay open across an outside call (2026-11-09)

- **What happened:** the live site started answering "database is locked" and the container went unhealthy. Every request failed at its first database step (`authenticate`).
- **Cause:** the engine opens every transaction with `BEGIN IMMEDIATE`, which takes SQLite's single write lock (a 2024 decision, see `db.py`). A signed-in request's first query started a transaction that stayed open until the request ended. The search page then waited up to 10 s on TMDB with that lock held, and search-as-you-type sends several such requests at once, so every other request (and the container's own health check) queued behind them for more than the 5 s wait and failed. This broke the plan's own rule that network calls never run inside a transaction.
- **A second case, worse:** the Rostercon refresh read the watched people and then spent minutes fetching pages, 10 s apart, with that transaction still open. Pressing "Refresh now" would have locked everyone out for the whole run.
- **Fix:** `AuthService.authenticate` commits after its select, so the lock is released before the handler goes on to talk to anything else; the refresh commits right after reading the watched people. Nothing else changes: later queries begin their own short transactions.
- **Tests (`test_no_lock_during_network.py`):** with a TMDB stand-in held for 12 s (longer than the 5 s wait), another user's page and `/health` must still answer in under 3 s while a search, a watch of a person and a watch of a film are each in flight; and a second connection can write while a refresh waits on Rostercon. Each test fails with the exact production error if its fix is removed.
- **Rule for new code:** never await an outside service (HTTP, a subprocess, a long sleep) while a session has a transaction open. Read what is needed, commit or close, do the outside work, then open a new transaction to write.
- **Not changed, but noted:** reads also take the write lock (every transaction is `BEGIN IMMEDIATE`). That is fine while transactions are short; if the app grows, a read-only connection with a plain `BEGIN` would remove the contention altogether.

## Review guests listed a person's own IMDb ids (2026-11-09)

- **Found by the project owner** on the live site: the "Linked guests" list showed `nm0000408` (Jonathan Frakes) and `nm0209496` (John de Lancie). Those are IMDb ids that watching a person records, not convention guests.
- **Cause:** the lists excluded only provider `tmdb`; watching a person also records an `imdb` identity, and both are rows in the same table as guests. An **Unlink** on one would have removed the person's IMDb link.
- **Fix:** a guest is now defined as an identity that appears at a convention (it has an appearance), not by provider name, so no future id type can leak in. The list, the search, and every decision (confirm, reject, unlink) use that definition; a decision on anything else is refused (404) and changes nothing, even if the request is crafted by hand.
- **Tests:** a person's own TMDB and IMDb ids are never listed or found, and cannot be unlinked, confirmed or rejected; with the guard removed each of those fails. Mutation checks cover the two lists and the two guards.
- **Checked live:** the two identities are still linked to their people (nothing was unlinked), see the commit notes.
## Logging with structlog (2026-11-09)

- **Why:** a refresh stopped early with only "Rostercon could not be read" and nothing in the log said why. The app logged almost nothing, as free text.
- **Decision (project owner):** use structlog, and log everywhere useful.
- **Design:** structlog sits over the standard library's logging (`ProcessorFormatter`), so Alembic, APScheduler and Granian lines share one stream and look. JSON when stdout is not a terminal (Docker), coloured console otherwise; `CASTTRAIL_LOG_LEVEL` and `CASTTRAIL_LOG_FORMAT` override. Noisy libraries are held at warning.
- **Safety:** a `redact` processor blanks any key containing password, secret, token, authorization, cookie or api_key. The request line holds the path only: never the query string, headers, cookies or body. Outside calls log the path only.
- **Correlation:** `RequestLogMiddleware` (outermost) gives each request an id (a caller's short alphanumeric `X-Request-ID` is kept) and returns it. `get_current_user` binds `user_id`; a refresh binds `job_id`; the background task binds `task`. All via contextvars, so no call site passes them. This also avoids reading an expired ORM object after a rollback.
- **Events:** requests; startup, migration and backup; refresh start/stop (with reason and cause)/finish with counts, each person matched, unmatched or ambiguous, each event page fetched or unreadable; Rostercon and TMDB requests, spacing and cooldowns; sign-in, password, account, identity, merge and watch actions.
- **Tests:** `tests/unit/test_logging.py` (format, level, redaction, quiet libraries, idempotence, request id, no query string); the old log-text tests now read events with `capture_logs()`.

## UI: Bootstrap 5.3 instead of Pico (2026-11-09)

- **Choice (project owner):** Bootstrap 5.3, not the 6.0 alpha. Bootstrap 6 is `6.0.0-alpha.1` (its own docs call it an alpha, and 5.3 is still marked "Latest"), so a stable release was used. Issue #50 asked whether to switch at all; the answer was yes.
- **Vendored, no CDN at runtime:** `bootstrap@5.3.8` CSS and the JS bundle (with Popper, for dropdowns and the collapsing menu), checked byte for byte against the SHA-256 hashes jsDelivr publishes for that release. Recorded in `static/VENDORED.md`. Pico and `nav.js` (the hand-written burger menu) are deleted.
- **What changed:** all 19 templates, `app.css` (now about 50 lines, mostly poster sizing) and the page tests that checked old markup. No route, query or behaviour changed.
- **Navigation:** one row on a desktop (it used to wrap to two, with a large Sign out box). Watchlist, My appearances, Events and Search are links; the five admin pages sit in one **Admin** dropdown (admins only); the user's name opens a menu with Dashboard, Change password, Status and Sign out. The current page keeps `aria-current="page"`. On a phone Bootstrap's own collapse replaces the old script.
- **Tables on a phone:** the Events and Appearances tables put the date, place and reason under each event's name and hide those columns below a tablet's width, so Status, Guests and Match are readable without a sideways scroll. Other tables sit in `.table-responsive` and scroll. Checked on a 390 px viewport: no page is wider than the screen.
- **Dark mode:** Bootstrap 5.3's `data-bs-theme`, set by a tiny `theme.js` loaded before the stylesheet so a dark device does not flash white, and following the device live. Pico did this by itself; Bootstrap needs the script.
- **Costs accepted:** the nav now needs JavaScript on a phone (the menu button and the dropdowns are Bootstrap's); without it the desktop links still show but the dropdown menus do not open. The page is about 230 KB of CSS and 80 KB of JS, against Pico's 83 KB of CSS; served locally and cached, so it only matters on the first visit.
- **Verified in a real browser** (Playwright, against a scratch database of sample data, not the live one): all 12 signed-in pages load with no console errors; the phone menu opens (`aria-expanded` turns true); the Admin dropdown opens; search-as-you-type still returns results with posters and Watch buttons; dark mode applies. Before and after screenshots of ten pages were compared.
- **Tests:** the old menu tests are replaced with ones for the collapse button, the account and admin menus (an ordinary user sees no admin link), the single current-page marker, the theme script loading first, and that Pico and `nav.js` are gone.
- **Not done:** the problem page shown at startup (`startup.py`) stays a standalone page with its own inline style, because it must work when nothing else does.

## Finding a person on Rostercon by asking for their page (2026-11-09)

- **Why:** the sitemap walk (decision above, `PeopleIndex`) read up to 20 files at 10 s apart, over 4 minutes and about 4.5 MB, before any person could be matched. Rostercon now has more than 22 sitemap files, so the 20-file cap cut the list short without saying so, and a person in the missing files was reported "not found". A single failure in the walk also stopped the whole run.
- **Checked with two single requests (2026-11-09, honest user agent):** `/en/people/jonathan-frakes-en` answers 200 and `/en/people/zzz-no-such-person-en` answers 404, so a missing page is a clean signal.
- **Decision:** `find_people(fetcher, name)` asks for the page directly. The name becomes a slug (lower-case ASCII, hyphens; it cannot hold a path, query or host). For each number 1, 2, 3... it tries `<slug>-en` then `<slug>` (`<slug>-2-en` then `<slug>-2` for the second person with that name) and uses the first that exists. It stops at the first number with no page, or after 5. None found is unmatched, one is the answer, several are ambiguous and are not guessed, the same rules as before.
- **Cost:** 3 to 4 requests to find a person, instead of the whole sitemap once; the page it finds is the person's page, so it is not fetched twice. At 10 s spacing that is about 30 s per newly seen person. Nothing is cached: a person's lookups repeat on each refresh, which keeps the logic simple and costs about the same as the event pages that follow.
- **Failure handling:** a refusal (429/503) still stops the run and starts the cooldown. Any other error while looking up one person (a 500, a timeout) counts that person as failed and the run goes on; before, one bad sitemap file ended the run.
- **Removed:** `PeopleIndex`, `parse_people_sitemap`, `match_people` and the sitemap fixture. Nothing else used them.
- **Not changed:** an existing page that Rostercon renames or moves is, as before, "not found" until it is found under its new name; a missing page is never taken to mean anything else.

## WP-5.5b: fetching Comiconomicon and remembering who was found (2026-11-13)

- **One fetcher for both scraped sites (project owner's choice):** the spacing (10 s), backoff on 429 and 503, 5 MB body cap, no redirects and host check moved from the Rostercon client into `PoliteFetcher` (`providers/polite.py`). Each source subclasses it with its host, label and its own error types, so a refusal or cooldown on one site is never taken for the other. `RosterconFetcher` behaves as before. Log events are now `source_request`, `source_refused`, `source_cooldown_started` and so on, each with a `source` key (they were `rostercon_*`).
- **Searching:** `comiconomicon_client.search` sends the name to the site's search box (`term` is encoded, so a `&` cannot start another parameter); an empty or over-100-character name sends nothing. `find_guests` keeps only guests whose whole name matches (5.5a's matcher). The log holds the request path only, so the name searched for is never logged.
- **Remembering answers (`source_lookups`, migration 0014):** one row per source and watched person, holding the outcome (`found`, `not_found` or `ambiguous`), the guest's key and address when found, and when it was asked. A `found` row must have both key and address, and no other outcome may have one; deleting the person deletes the row.
- **How long an answer is trusted (project owner agreed):** found 180 days (a guest's id does not change), not found 7 days (a guest may be added once their appearances are announced), ambiguous 30 days. Time is read from the clock (`utc_now`), so tests move it with `time-machine`.
- **Failures:** a refusal, an unreadable answer or a timeout stores nothing, and an older answer is kept, so "the site is down" is never remembered as "no such guest".
- **No lock across the network:** `GuestLookup.find` commits after reading the stored answer and before asking the site, then begins a new transaction to write (the rule from the "database is locked" decision). A test checks no transaction is open when the request is made.
- **Not done in this chunk:** nothing calls `GuestLookup` yet. 5.5c refreshes from the guest pages found, as a scheduled job and a button. A manual link (plan A10) will reuse this table.

## A10: linking a person to a Rostercon page by hand (2026-11-13)

- **Why:** the first live refresh with the direct lookup could not place Thomas F. Wilson or Kirsten Cooke. Their pages, if they exist, are not at an address built from the TMDB name (Rostercon likely says *Tom*), and there was no way to tell CastTrail where they are.
- **What:** an admin page, **Rostercon pages** (`/admin/links`), lists every watched person with where they stand (set by hand, found by name, not found, several pages, not looked up yet) and takes a pasted address. Removing a link sends the person back to lookup by name.
- **Stored in `source_lookups`** (migration 0015 adds `manual`). A manual row is always a found row, never goes stale, and is never replaced by a search: the refresh skips writing when a link is set, and `GuestLookup` re-reads the row after waiting on the site so a link set meanwhile survives. This follows the rule that manual decisions are never overwritten by a sync.
- **The pasted address is untrusted:** `parse_person_link` accepts only `https`, Rostercon's host (with or without `www`), and `/en/people/<lower-case words and hyphens>`. It rebuilds the address from that name part, so nothing else in the paste (a query, a fragment, another host, a path) can reach a request, and the fetcher still refuses any other host.
- **Not checked when saved:** the page is not fetched to see that it exists. The request would wait behind the fetcher's 10 s spacing (and a whole refresh, if one is running) while holding a page open. A mistyped name is found at the next refresh instead, where it is counted as a failed person and logged (`person_link_failed`), with no fallback to guessing; the link stays so it can be fixed.
- **A refresh now remembers its answers** for people it finds by name: found (with the page), not found, or several pages. These are for display only; they do not make a refresh skip the lookup. A failed lookup (a 500, a timeout) stores nothing, so an outage is never recorded as "not found".
- **Not done:** the Source health warning still does not name the people it skipped (plan A9), though this page now shows them. Only Rostercon has a page for this; Comiconomicon manual links would use the same table.

## A12: reuse known Rostercon pages, and say when a restart cut a run short (2026-11-13)

- **What went wrong (from the live run history):** three refreshes in 25 minutes made roughly 4 requests per person (the page, two probes for a second person with the same name, and the event list), so a 17-person run made about 68 requests against Rostercon's limit of about 25 in 30 minutes. The last of them was refused (503) after 15 people. Separately, a run that a container replacement killed was recorded as "The refresh crashed" (the image was built two seconds before the run ended), which sent us looking for a bug that was not there.
- **Decision:** a page found by name is stored (A10) and now also *used*. `RosterconLinks.known` gives a refresh a person's stored page if it was set by hand (always) or found by name less than 30 days ago; the refresh asks for that page directly (1 request) and skips the probes. After 30 days the name is looked up again, because that lookup is also what notices that a second person has been given the same name. This supersedes the A10 note that remembered answers are for display only.
- **When the known page fails:** if a page found by name is now missing (404), the name is looked up again and the new answer stored. Any other error counts that person as failed and leaves the stored page alone, a refusal (429/503) stops the run, and a page set by hand is never replaced by a guess (a 404 on it is a failed person).
- **Not known, so looked up every run:** people not found or with several pages. Their lookups cost the same as before; the admin page now shows who they are (A10).
- **Interrupted, not crashed:** cancellation (the app stopping) is recorded as "Interrupted: the app stopped", logged as a warning (`refresh_interrupted`) without a traceback, and the lock is released as before. Anything else is still "The refresh crashed" with the traceback.
- **Not done:** a stopped run still starts again from the first person. Plan A13 covers it: look up people not refreshed for longest first, and cap the people per run, so a long list always makes progress.

## A14: one Source pages page for every source (2026-11-13)

- **Why (project owner):** the Rostercon pages page only made sense for one site, and Comiconomicon (and maybe Wikipedia or others later) need the same thing: a way to say which page is a person's when the name does not find it.
- **Design:** a source is a `LinkSource` (its provider id, display name, an example address, and a parser that checks a pasted address). `SourceLinks(session, source)` does what `RosterconLinks` did, for any source, on the same `source_lookups` table (one row per source and person). `ROSTERCON` and `COMICONOMICON` are defined in `discovery/links.py`; adding a source is adding one more, plus its parser in `providers/`.
- **The page** (`/admin/links`, in the Admin menu as **Source pages**) has a column per source. Each cell shows the state (set by hand, found by name, not found, several pages, not looked up yet), the page, a field to paste an address, and a remove button for a link set by hand. The notice for a refused address names the source and shows its example. An unknown source in a form is a 404.
- **Each source checks its own addresses.** A Rostercon address is refused for Comiconomicon and the other way round, and neither can be stored under the wrong source. `parse_guest_link` is as strict as `parse_person_link`: `https`, the site's own host (with or without `www`), `/guest/<number>` and an optional plain name, rebuilt from the checked path.
- **Same rules everywhere:** a link set by hand never goes stale and is never replaced by a lookup (`GuestLookup` re-reads the row after waiting on the site, as for Rostercon); only an admin can remove it; a refresh's own answers are never removed from this page.
- **Changed names:** log events are `source_link_set` and `source_link_removed` with a `source` key (they were `rostercon_link_*`). `RosterconLinks` and `RosterconLink` became `SourceLinks` and `SourceLink`; a page is a `LinkTarget(key, url)` (a Rostercon `slug` is its `key`).
- **Not done:** only Rostercon's refresh uses the manual link so far. Comiconomicon's refresh does not exist yet (plan 5.5c); when it does it goes through `GuestLookup`, which already honours these links.

## A15: source links on the review page (2026-11-13)

- **Why (project owner):** a reviewer confirming that a guest is a watched person has to see the guest on the source, not just a name and an id.
- **What:** each row on **Review guests** (waiting, linked and search results) links the guest's id to its page: `https://www.rostercon.com/en/people/<name>` or `https://comiconomicon.com/guest/<number>`. The links open in a new tab with `noopener noreferrer`.
- **The address is built, not stored.** It is made from the source and the guest's key (`page_url_for`, which asks that source's own builder: `person_url` for Rostercon, `guest_url` for Comiconomicon), and only when the key is a plain one (a lower-case name with hyphens, or a number of up to 9 ASCII digits). A key that is not plain, or a source we do not link to (TMDB, IMDb), is shown as text with no link, so imported data can never put an address on the page.
- **Checked against the live site (2026-11-13, two single requests, honest user agent):** `comiconomicon.com/guest/518` and `/guest/518/Andrew_Jackson` both answer 200, so the number alone is enough. Rostercon's `/en/people/<name>` is the form the refresh already fetches.

