import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser

from casttrail.providers.base import ConventionInfo, GuestObservation, ListingResult

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
