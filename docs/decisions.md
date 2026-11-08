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
