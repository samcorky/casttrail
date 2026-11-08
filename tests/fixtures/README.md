# Fixtures

Trimmed snapshots of third-party pages, fetched on 2026-11-08 for parser tests only.
Tests never touch the live network. Each file keeps only the data block a parser needs
(Rostercon: the JSON-LD; Comiconomicon: the event and guest sections) and starts with a
comment giving its source URL. Do not add whole pages: the sources reserve their content.

| Path | Used for |
|---|---|
| `rostercon/event_*.html` | `Event` JSON-LD: dates, status, address, performers |
| `rostercon/people_*.html` | `Person` JSON-LD |
| `comiconomicon/event_*.html` | event date, location, site link, guest list |
| `comiconomicon/videomedia_*.html` | discovery by show page |

`event_follow-the-yellow-gate-road-take-4` (Rostercon) and `event_42676_...` (Comiconomicon)
are the same real event, for the WP-5 dedupe test.
