import html
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from casttrail.providers.base import (
    ConventionInfo,
    EventStatus,
    GuestObservation,
    ListingResult,
)

PROVIDER_ID = "rostercon"
HOST = "www.rostercon.com"
EVENT_PATH = "/en/event-convention/"
PERSON_PATH = "/en/people/"
LOCATION_PATH = "/en/location/"

_STATUSES: dict[str, EventStatus] = {
    "EventScheduled": "scheduled",
    "EventCancelled": "cancelled",
    "EventPostponed": "postponed",
    "EventRescheduled": "postponed",
}


@dataclass(frozen=True)
class ParsedEvent:
    """What one Rostercon event page said.

    ``convention`` is ``None`` when the page held no event at all, which is also the
    sign of a page that has changed shape. ``guests.complete`` says whether the guest
    list can be trusted as the whole list.
    """

    convention: ConventionInfo | None
    guests: ListingResult


class _LdJson(HTMLParser):
    """Collects the text of every ``application/ld+json`` script block."""

    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[str] = []
        self._inside = False
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("type") == "application/ld+json":
            self._inside = True
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._inside:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._inside:
            self._inside = False
            self.blocks.append("".join(self._text))


def _nodes(html: str) -> list[dict[str, Any]]:
    """Return every JSON-LD node on the page, flattening ``@graph`` and lists."""
    collector = _LdJson()
    collector.feed(html)
    nodes: list[dict[str, Any]] = []
    for block in collector.blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        for item in stack:
            if isinstance(item, dict):
                graph = item.get("@graph")
                nodes.extend(
                    n
                    for n in (graph if isinstance(graph, list) else [item])
                    if isinstance(n, dict)
                )
    return nodes


def _is_event(node: dict[str, Any]) -> bool:
    kind = node.get("@type")
    return kind == "Event" or (isinstance(kind, list) and "Event" in kind)


def _text(value: object) -> str | None:
    """Read a text value from the page's JSON data, with its HTML entities decoded.

    WordPress writes ``&`` as ``&#038;`` and ``'`` as ``&#8217;`` even inside the JSON,
    where an HTML parser does not decode them. They are decoded here, once, and the
    result is still only text: it is shown escaped, never as markup.
    """
    if not isinstance(value, str):
        return None
    return html.unescape(value).strip() or None


def _day(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _slug(url: object, prefix: str) -> str | None:
    """Return the last part of a Rostercon URL, or ``None`` if it is not one of ours.

    The address comes from outside, so it must be https on Rostercon's own host and
    under the expected path. Anything else is refused rather than stored.
    """
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != HOST
        or not parts.path.startswith(prefix)
    ):
        return None
    slug = parts.path.removeprefix(prefix).strip("/")
    return slug if slug and "/" not in slug else None


def _country(location: dict[str, Any]) -> str | None:
    """Read the country from the location's URL.

    The address's own ``addressCountry`` is written in French ("Royaume-Uni"), so it
    cannot be compared with anything. The URL carries a stable slug such as
    ``united-kingdom``.
    """
    url = location.get("url")
    if not isinstance(url, str):
        return None
    path = urlsplit(url).path
    if not path.startswith(LOCATION_PATH):
        return None
    country = path.removeprefix(LOCATION_PATH).split("/")[0]
    return country or None


def _convention(event: dict[str, Any]) -> ConventionInfo | None:
    url = event.get("url")
    key = _slug(url, EVENT_PATH)
    name = _text(event.get("name"))
    if key is None or name is None or not isinstance(url, str):
        return None
    location = event.get("location")
    location = location if isinstance(location, dict) else {}
    address = location.get("address")
    address = address if isinstance(address, dict) else {}
    organizer = event.get("organizer")
    status = event.get("eventStatus")
    return ConventionInfo(
        source_key=key,
        name=name,
        source_url=url,
        start_date=_day(event.get("startDate")),
        end_date=_day(event.get("endDate")),
        venue=_text(address.get("streetAddress")),
        city=_text(address.get("addressLocality")),
        region=_text(address.get("addressRegion")),
        country=_country(location),
        organizer=_text(organizer.get("name")) if isinstance(organizer, dict) else None,
        status=(
            _STATUSES.get(status.rsplit("/", 1)[-1])
            if isinstance(status, str)
            else None
        ),
    )


def _guests(event: dict[str, Any], etag: str | None) -> ListingResult:
    performers = event.get("performer")
    if performers is None:
        # No list at all is how an event with no guests yet looks, but it is also how a
        # page that has changed shape looks, so it is not trusted as the whole list.
        return ListingResult(items=[], complete=False, etag=etag)
    if isinstance(performers, dict):
        performers = [performers]
    if not isinstance(performers, list):
        return ListingResult(items=[], complete=False, etag=etag)
    guests: dict[str, GuestObservation] = {}
    complete = True
    for performer in performers:
        name = _text(performer.get("name")) if isinstance(performer, dict) else None
        url = performer.get("url") if isinstance(performer, dict) else None
        key = _slug(url, PERSON_PATH)
        if name is None or key is None or not isinstance(url, str):
            complete = False
            continue
        guests.setdefault(
            key, GuestObservation(source_key=key, name=name, source_url=url)
        )
    return ListingResult(items=list(guests.values()), complete=complete, etag=etag)


def parse_event_page(html: str, *, etag: str | None = None) -> ParsedEvent:
    """Read a Rostercon event page.

    Never raises on bad input: a page that has changed shape comes back as no
    convention and an incomplete, empty guest list.

    Args:
        html: The page's HTML.
        etag: The ``ETag`` the page was served with, passed through unchanged.

    Returns:
        The convention and its guest list.
    """
    event = next((node for node in _nodes(html) if _is_event(node)), None)
    if event is None:
        return ParsedEvent(None, ListingResult(items=[], complete=False, etag=etag))
    convention = _convention(event)
    if convention is None:
        return ParsedEvent(None, ListingResult(items=[], complete=False, etag=etag))
    return ParsedEvent(convention, _guests(event, etag))


# --- event lists, person pages and the people index ---------------------------------

_ROW_FIELDS = (
    "date-list-event",
    "name-list-event",
    "location-list-event",
    "serie-list-event",
)
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
_DATE = re.compile(r"\b([A-Z][a-z]{2}) (\d{1,2})(?:st|nd|rd|th) (\d{4})\b")
_EVENTS_LINK = re.compile(r"/en/people/[^/]+/events/\d+")


@dataclass(frozen=True)
class ListedEvent:
    """One row of an event list: a person's, a show's or a country's."""

    source_key: str
    source_url: str
    name: str
    start_date: date | None
    place: str | None
    show: str | None


@dataclass(frozen=True)
class EventRows:
    """The events found in a list page, and how many rows could not be read."""

    events: list[ListedEvent]
    skipped: int


@dataclass(frozen=True)
class PersonPage:
    """A person Rostercon has a page for."""

    slug: str
    url: str


class _RowReader(HTMLParser):
    """Reads the ``<ul class="list-inline row">`` rows of an event list page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, tuple[str, str | None]]] = []
        self._row: dict[str, tuple[str, str | None]] | None = None
        self._field: str | None = None
        self._text: list[str] = []
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag == "ul" and "list-inline" in classes and "row" in classes:
            self._row = {}
        elif self._row is not None and tag == "li":
            for field in _ROW_FIELDS:
                if field in classes:
                    self._field, self._text, self._href = field, [], None
        elif self._field == "name-list-event" and tag == "a" and self._href is None:
            self._href = values.get("href")

    def handle_data(self, data: str) -> None:
        if self._field is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" and self._field is not None and self._row is not None:
            text = " ".join("".join(self._text).split())
            self._row[self._field] = (text, self._href)
            self._field = None
        elif tag == "ul" and self._row is not None:
            if "date-list-event" in self._row:
                self.rows.append(self._row)
            self._row = None


def _listed_date(text: str) -> date | None:
    found = _DATE.search(text)
    if found is None or found.group(1) not in _MONTHS:
        return None
    try:
        return date(int(found.group(3)), _MONTHS[found.group(1)], int(found.group(2)))
    except ValueError:
        return None


def parse_event_rows(html: str) -> EventRows:
    """Read the events listed on a Rostercon list, person or show page.

    Never raises on bad input. A row with no usable name or event address is counted
    in ``skipped`` so a changed page shows up as a rising number, not as silence.

    Args:
        html: The page's HTML.

    Returns:
        The events in page order, and the number of rows that could not be read.
    """
    reader = _RowReader()
    reader.feed(html)
    events: list[ListedEvent] = []
    skipped = 0
    for row in reader.rows:
        name, href = row.get("name-list-event", ("", None))
        key = _slug(href, EVENT_PATH)
        if key is None or not name or href is None:
            skipped += 1
            continue
        events.append(
            ListedEvent(
                source_key=key,
                source_url=href,
                name=name,
                start_date=_listed_date(row.get("date-list-event", ("", None))[0]),
                place=row.get("location-list-event", ("", None))[0] or None,
                show=row.get("serie-list-event", ("", None))[0] or None,
            )
        )
    return EventRows(events=events, skipped=skipped)


def parse_all_events_link(html: str) -> str | None:
    """Find the link to a person page's full list of events.

    Args:
        html: A person page's HTML.

    Returns:
        The address, or ``None`` if there is none on Rostercon's own host.
    """
    for found in re.finditer(r'href="([^"]+)"', html):
        parts = urlsplit(found.group(1))
        if (
            parts.scheme == "https"
            and parts.netloc == HOST
            and not parts.query
            and _EVENTS_LINK.fullmatch(parts.path)
        ):
            return f"https://{HOST}{parts.path}"
    return None


def slugify_name(name: str) -> str:
    """Turn a name into the form Rostercon's addresses use.

    Args:
        name: A person's name.

    Returns:
        Lower-case ASCII words joined by hyphens, with accents removed.
    """
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")


_PERSON_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_MAX_SLUG = 120


def parse_person_link(text: str) -> PersonPage | None:
    """Read a Rostercon person page address that someone pasted in.

    Only an ``https`` address on Rostercon's own host, for ``/en/people/<name>``, is
    accepted. The result is rebuilt from the name part, never copied, so nothing else in
    what was pasted (a query, a fragment, a different host) can reach a request.

    Args:
        text: What was pasted. Surrounding spaces are ignored.

    Returns:
        The page, or ``None`` if this is not the address of one person's page.
    """
    try:
        parts = urlsplit(text.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or host not in (HOST, HOST.removeprefix("www."))
        or parts.username is not None
        or port is not None
        or not parts.path.startswith(PERSON_PATH)
    ):
        return None
    slug = parts.path.removeprefix(PERSON_PATH).removesuffix("/")
    if len(slug) > _MAX_SLUG or not _PERSON_SLUG.fullmatch(slug):
        return None
    return PersonPage(slug=slug, url=f"https://{HOST}{PERSON_PATH}{slug}")
