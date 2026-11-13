from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from datetime import timedelta

MediaType = Literal["movie", "tv"]
EventStatus = Literal["scheduled", "cancelled", "postponed"]


class ProviderError(Exception):
    """A provider could not give a usable answer.

    The message never contains credentials or URLs, so it is safe to log and show.
    """

    def __init__(self, message: str, *, retry_after: timedelta | None = None) -> None:
        """Create the error.

        Args:
            message: What went wrong, without secrets.
            retry_after: How long the provider asked callers to wait, if it said.
        """
        super().__init__(message)
        self.retry_after = retry_after


class _Data(BaseModel):
    model_config = ConfigDict(frozen=True)


class PersonSummary(_Data):
    """A person as returned by a search."""

    tmdb_id: int
    name: str
    known_for_department: str | None = None
    profile_path: str | None = None


class PersonDetail(PersonSummary):
    """A person with the identifiers needed to match them across sources."""

    imdb_id: str | None = None


class Production(_Data):
    """A film or a series."""

    tmdb_id: int
    media_type: MediaType
    title: str
    release_year: int | None = None
    overview: str | None = None
    poster_path: str | None = None


class CastMember(_Data):
    """One person billed on a production. A lower ``order`` is higher billing."""

    person: PersonSummary
    character: str | None
    order: int


class PersonCredit(_Data):
    """A production a person appears in."""

    production: Production
    character: str | None = None
    order: int | None = None


class ConventionInfo(_Data):
    """One convention as a listing source describes it.

    ``None`` means the source did not say. A missing status is never a cancellation:
    only ``status="cancelled"``, stated by the source, is one.
    """

    source_key: str
    name: str
    source_url: str
    start_date: date | None = None
    end_date: date | None = None
    venue: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None
    organizer: str | None = None
    status: EventStatus | None = None


class GuestObservation(_Data):
    """One guest a source lists for a convention."""

    source_key: str
    name: str
    source_url: str
    identifiers: dict[str, str] = {}
    status: EventStatus | None = None


class ListingResult(_Data):
    """The guests a source listed for a convention at one moment.

    ``complete`` is ``True`` only when the whole listing was fetched and understood.
    Anything less (a partial fetch, a page that no longer parses, an HTTP error) is
    ``False``, and the caller must then keep what it already knows: a guest missing
    from an incomplete listing says nothing about whether they are still attending.
    """

    items: list[GuestObservation]
    complete: bool
    etag: str | None = None


class MetadataProvider(Protocol):
    """Where people, films and series come from.

    It returns data and never touches the database.

    Every method raises ``ProviderError`` when the source cannot give a usable answer.
    A thing the source says does not exist comes back as ``None``.
    """

    provider_id: str

    async def search_people(self, query: str) -> list[PersonSummary]:
        """Find people by name."""
        ...

    async def search_productions(self, query: str) -> list[Production]:
        """Find films and series by title."""
        ...

    async def production(
        self, media_type: MediaType, tmdb_id: int
    ) -> Production | None:
        """Return one film or series, or ``None`` if the source has no such thing."""
        ...

    async def person(self, tmdb_id: int) -> PersonDetail | None:
        """Return one person, or ``None`` if the source has no such person."""
        ...

    async def person_credits(self, tmdb_id: int) -> list[PersonCredit] | None:
        """Return what a person has appeared in, or ``None`` if there is no one."""
        ...

    async def cast(
        self, media_type: MediaType, tmdb_id: int, *, limit: int = 10
    ) -> list[CastMember] | None:
        """Return the top-billed cast, or ``None`` if there is no such production."""
        ...


@dataclass(frozen=True)
class LinkTarget:
    """A page on a source that a person can be linked to: its key and its address."""

    key: str
    url: str
