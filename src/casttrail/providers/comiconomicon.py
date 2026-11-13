import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urlsplit

from casttrail.providers.base import (
    ConventionInfo,
    GuestObservation,
    LinkTarget,
    ListingResult,
    ProviderError,
)

PROVIDER_ID = "comiconomicon"
HOST = "comiconomicon.com"
BASE = f"https://{HOST}"

# Addresses are rebuilt from a validated path, never copied from the page.
_PATH = r"/{kind}/(\d{{1,9}})(?:/[A-Za-z0-9_.%()~-]{{0,200}})?"
_EVENT_PATH = re.compile(_PATH.format(kind="event"))
_GUEST_PATH = re.compile(_PATH.format(kind="guest"))
_VIDEOMEDIA_PATH = re.compile(_PATH.format(kind="videomedia"))

_MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ),
        start=1,
    )
}
_PART = re.compile(r"(\d{1,2})(?:\s+([A-Z][a-z]{2}))?(?:,?\s*(\d{4}))?")
_UK = "united-kingdom"
_UK_ENDINGS = {
    "united kingdom": None,
    "england": "England",
    "scotland": "Scotland",
    "wales": "Wales",
    "northern ireland": "Northern Ireland",
}


@dataclass(frozen=True)
class ListedEvent:
    """One event in a list on a guest, film or series page."""

    source_key: str
    source_url: str
    name: str
    start_date: date | None
    end_date: date | None
    location: str | None


@dataclass(frozen=True)
class Role:
    """A film or series a guest has been in, as the guest page lists it."""

    source_key: str
    source_url: str
    title: str
    role: str | None
    detail: str | None


@dataclass(frozen=True)
class EventList:
    """The events found on a page, and how many blocks could not be read."""

    events: list[ListedEvent]
    skipped: int


@dataclass(frozen=True)
class GuestPage:
    """What a guest page says: where they appear and what they have been in."""

    events: list[ListedEvent]
    roles: list[Role]
    skipped: int


@dataclass(frozen=True)
class ParsedEvent:
    """What one event page said.

    ``convention`` is ``None`` when the page held no readable event, which is also the
    sign of a page that has changed shape.
    """

    convention: ConventionInfo | None
    guests: ListingResult


def _path_id(href: str | None, pattern: re.Pattern[str]) -> tuple[str, str] | None:
    """Return the id and the checked path of a link to our own site, or ``None``."""
    if href is None or not pattern.fullmatch(href):
        return None
    found = pattern.fullmatch(href)
    assert found is not None
    return found.group(1), href


def _clean(text: str) -> str:
    return " ".join(text.split())


def _parse_part(text: str) -> tuple[int, int | None, int | None] | None:
    found = _PART.fullmatch(text.strip())
    if found is None:
        return None
    month = _MONTHS.get(found.group(2)) if found.group(2) else None
    if found.group(2) and month is None:
        return None
    year = int(found.group(3)) if found.group(3) else None
    return int(found.group(1)), month, year


def parse_dates(text: str) -> tuple[date | None, date | None]:
    """Read Comiconomicon's date text, such as ``17 - 18 Apr, 2027``.

    Args:
        text: The text of an event's date line.

    Returns:
        The first and last day, or ``(None, None)`` if the text is not understood. A
        single day gives the same date twice.
    """
    parts = [_parse_part(piece) for piece in text.split(" - ")]
    if not 1 <= len(parts) <= 2 or any(part is None for part in parts):
        return None, None
    last = parts[-1]
    assert last is not None
    day_end, month_end, year_end = last
    if month_end is None or year_end is None:
        return None, None
    first = parts[0]
    assert first is not None
    day_start, month_start, year_start = first
    month_start = month_start or month_end
    if year_start is None:
        year_start = year_end - 1 if month_start > month_end else year_end
    try:
        return (
            date(year_start, month_start, day_start),
            date(year_end, month_end, day_end),
        )
    except ValueError:
        return None, None


def _place(location: str | None) -> tuple[str | None, str | None]:
    """Return the country slug and region, when the location ends in a UK name."""
    if not location:
        return None, None
    ending = location.rstrip(" .,").lower()
    for suffix, region in _UK_ENDINGS.items():
        if ending == suffix or ending.endswith((f" {suffix}", f",{suffix}")):
            return _UK, region
    return None, None


class _PageReader(HTMLParser):
    """Collects events, guests and roles from Comiconomicon's page markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[dict[str, str | None]] = []
        self.guests: list[dict[str, str | None]] = []
        self.roles: list[dict[str, str | None]] = []
        self._field: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "a":
            self._link(classes, values)
        elif tag == "div":
            self._block(classes)

    def _link(self, classes: set[str], values: dict[str, str | None]) -> None:
        if "eventname" in classes and self.events:
            self.events[-1].update(
                id=values.get("data-id"),
                name=values.get("data-name"),
                href=values.get("href"),
            )
        elif "eventguest" in classes:
            self.guests.append(
                {
                    "id": values.get("data-id"),
                    "name": values.get("data-name"),
                    "href": values.get("href"),
                }
            )
        elif "rolevideomedianame" in classes and self.roles:
            self.roles[-1].update(href=values.get("href"), id=values.get("data-id"))
            self._begin("title")

    def _block(self, classes: set[str]) -> None:
        if "eventsearchresult" in classes:
            self.events.append({})
        elif "role" in classes:
            self.roles.append({})
            self._begin("role")
        elif "rolemetatext" in classes and self.roles:
            self._begin("detail")
        elif "eventdate" in classes and self.events:
            self._begin("date")
        elif "eventlocation" in classes and self.events:
            self._begin("location")

    def _begin(self, field: str) -> None:
        self._field, self._text = field, []

    def handle_data(self, data: str) -> None:
        if self._field is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._field is None or tag not in ("div", "a"):
            return
        text = _clean("".join(self._text))
        target = (
            self.roles if self._field in ("role", "title", "detail") else self.events
        )
        target[-1][self._field] = text
        self._field = None


def _listed(row: dict[str, str | None]) -> ListedEvent | None:
    found = _path_id(row.get("href"), _EVENT_PATH)
    name = _clean(row.get("name") or "")
    if found is None or not name or row.get("id") != found[0]:
        return None
    start, end = parse_dates(row.get("date") or "")
    return ListedEvent(
        source_key=found[0],
        source_url=f"{BASE}{found[1]}",
        name=name,
        start_date=start,
        end_date=end,
        location=row.get("location") or None,
    )


def _events(reader: _PageReader) -> EventList:
    events: dict[str, ListedEvent] = {}
    skipped = 0
    for row in reader.events:
        event = _listed(row)
        if event is None:
            skipped += 1
        else:
            events.setdefault(event.source_key, event)
    return EventList(list(events.values()), skipped)


def parse_event_list(html: str) -> EventList:
    """Read the events on a film, series or guest page.

    Args:
        html: The page's HTML.

    Returns:
        The events, each once, and how many blocks could not be read.
    """
    reader = _PageReader()
    reader.feed(html)
    reader.close()
    return _events(reader)


def parse_guest_page(html: str) -> GuestPage:
    """Read a guest's page: their events and the films and series they were in.

    Args:
        html: The page's HTML.

    Returns:
        The events and roles, each once.
    """
    reader = _PageReader()
    reader.feed(html)
    reader.close()
    listed = _events(reader)
    roles: dict[str, Role] = {}
    for row in reader.roles:
        found = _path_id(row.get("href"), _VIDEOMEDIA_PATH)
        title = _clean(row.get("title") or "")
        if found is None or not title or row.get("id") != found[0]:
            continue
        roles.setdefault(
            found[0],
            Role(
                source_key=found[0],
                source_url=f"{BASE}{found[1]}",
                title=title,
                role=row.get("role") or None,
                detail=row.get("detail") or None,
            ),
        )
    return GuestPage(listed.events, list(roles.values()), listed.skipped)


def parse_event_page(html: str, *, etag: str | None = None) -> ParsedEvent:
    """Read a Comiconomicon event page.

    The guest list is never marked complete: the site says appearances come from
    automated matching and may change, so a guest missing from a later fetch says
    nothing about whether they are still attending.

    Never raises on bad input: a page that has changed shape comes back as no
    convention and an empty guest list.

    Args:
        html: The page's HTML.
        etag: The ``ETag`` the page was served with, passed through unchanged.

    Returns:
        The convention and its guests.
    """
    reader = _PageReader()
    reader.feed(html)
    reader.close()
    nothing = ListingResult(items=[], complete=False, etag=etag)
    event = next((e for e in map(_listed, reader.events) if e is not None), None)
    if event is None:
        return ParsedEvent(None, nothing)
    country, region = _place(event.location)
    convention = ConventionInfo(
        source_key=event.source_key,
        name=event.name,
        source_url=event.source_url,
        start_date=event.start_date,
        end_date=event.end_date,
        venue=event.location,
        region=region,
        country=country,
    )
    guests: dict[str, GuestObservation] = {}
    for row in reader.guests:
        found = _path_id(row.get("href"), _GUEST_PATH)
        name = _clean(row.get("name") or "")
        if found is None or not name or row.get("id") != found[0]:
            continue
        guests.setdefault(
            found[0],
            GuestObservation(
                source_key=found[0], name=name, source_url=f"{BASE}{found[1]}"
            ),
        )
    return ParsedEvent(
        convention,
        ListingResult(items=list(guests.values()), complete=False, etag=etag),
    )


# --- the site's own search box -------------------------------------------

# Where a name can be turned into the site's numbers. Not on the robots.txt list of
# paths, which says nothing against it; the project owner chose to use it (see
# docs/decisions.md). Low volume, polite spacing and caching are the fetcher's job.
AUTOCOMPLETE_PATH = "/xhr/indexautocomplete.php"
KINDS = ("guest", "videomedia", "event")
MAX_SUGGESTIONS = 100
_KIND_PATHS = {
    "guest": _GUEST_PATH,
    "videomedia": _VIDEOMEDIA_PATH,
    "event": _EVENT_PATH,
}


@dataclass(frozen=True)
class Suggestion:
    """One result of the site's search: a guest, a film or series, or an event."""

    kind: str
    source_key: str
    source_url: str
    name: str
    # Upcoming appearances the site counts for it.
    appearances: int
    # A film or series' year.
    year: int | None = None
    # An event's days and place.
    start_date: date | None = None
    end_date: date | None = None
    location: str | None = None


@dataclass(frozen=True)
class SuggestionList:
    """What a search returned, and how many entries could not be used."""

    items: list[Suggestion]
    skipped: int


def _year(value: object) -> int | None:
    text = str(value).strip() if value is not None else ""
    return int(text) if text.isascii() and text.isdigit() and len(text) == 4 else None


def _iso_day(value: object) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _suggestion(entry: object) -> Suggestion | None:
    if not isinstance(entry, dict):
        return None
    kind = entry.get("type")
    key, name, url = entry.get("id"), entry.get("name"), entry.get("url")
    if kind not in KINDS or not isinstance(name, str) or not isinstance(key, str):
        return None
    found = _path_id(url if isinstance(url, str) else None, _KIND_PATHS[kind])
    clean = _clean(name)
    if found is None or not clean or found[0] != key:
        return None
    appearances = entry.get("appearances")
    count = appearances if isinstance(appearances, int) and appearances > 0 else 0
    base = Suggestion(kind, key, f"{BASE}{found[1]}", clean, count)
    if kind == "videomedia":
        return Suggestion(**{**base.__dict__, "year": _year(entry.get("data1"))})
    if kind == "event":
        place = entry.get("data3")
        return Suggestion(
            **{
                **base.__dict__,
                "start_date": _iso_day(entry.get("data1")),
                "end_date": _iso_day(entry.get("data2")),
                "location": _clean(place) if isinstance(place, str) and place else None,
            }
        )
    return base


def parse_autocomplete(text: str) -> SuggestionList:
    """Read the answer to a search for a name.

    Each entry is checked like a link on a page: its kind must be one we know, and its
    address must be that kind's own path with the same number as its ``id``. The
    ``html`` field the site sends for its drop-down is ignored.

    Args:
        text: The response body, which should be a JSON array.

    Returns:
        The usable entries (at most ``MAX_SUGGESTIONS``, each once) and how many were
        not usable.

    Raises:
        ProviderError: If the body is not a JSON array, so a changed answer is noticed
            and not mistaken for "no results".
    """
    try:
        data = json.loads(text)
    except ValueError as error:
        msg = "Comiconomicon's search did not answer in the form we expect."
        raise ProviderError(msg) from error
    if not isinstance(data, list):
        msg = "Comiconomicon's search did not answer with a list."
        raise ProviderError(msg)
    found: dict[tuple[str, str], Suggestion] = {}
    skipped = 0
    for entry in data[:MAX_SUGGESTIONS]:
        item = _suggestion(entry)
        if item is None:
            skipped += 1
        else:
            found.setdefault((item.kind, item.source_key), item)
    return SuggestionList(
        list(found.values()), skipped + max(0, len(data) - MAX_SUGGESTIONS)
    )


def _plain(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9]+", " ", folded.lower().replace("&", " and "))
    return " ".join(words.split())


def match_by_name(
    items: list[Suggestion], kind: str, name: str, *, year: int | None = None
) -> list[Suggestion]:
    """Pick the search results that are exactly the thing being looked for.

    Names are compared whole, ignoring case, accents and punctuation. For a film or
    series a year, if given, must agree too (a result with no year does not match).

    Args:
        items: The results of a search.
        kind: ``guest``, ``videomedia`` or ``event``.
        name: The name looked for.
        year: A film or series' release year, if known.

    Returns:
        The matches: none means not found, one is the answer, and several mean it is
        ambiguous and the caller must not guess.
    """
    wanted = _plain(name)
    if not wanted:
        return []
    return [
        item
        for item in items
        if item.kind == kind
        and _plain(item.name) == wanted
        and (year is None or item.year == year)
    ]


def parse_guest_link(text: str) -> LinkTarget | None:
    """Read a Comiconomicon guest page address that someone pasted in.

    Only an ``https`` address on Comiconomicon's own host, for ``/guest/<number>`` with
    an optional plain name after it, is accepted. The result is rebuilt from the checked
    path, so nothing else in what was pasted (a query, a fragment, another host) can
    reach a request.

    Args:
        text: What was pasted. Surrounding spaces are ignored.

    Returns:
        The page, or ``None`` if this is not the address of one guest's page.
    """
    try:
        parts = urlsplit(text.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or host not in (HOST, f"www.{HOST}")
        or parts.username is not None
        or port is not None
    ):
        return None
    found = _path_id(parts.path, _GUEST_PATH)
    if found is None:
        return None
    return LinkTarget(key=found[0], url=f"{BASE}{found[1]}")
