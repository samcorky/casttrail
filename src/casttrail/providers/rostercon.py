import json
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
    return value.strip() or None if isinstance(value, str) else None


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
