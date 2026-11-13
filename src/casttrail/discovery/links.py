import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import delete, select

from casttrail.catalogue.models import Person
from casttrail.db import utc_now
from casttrail.discovery.models import SourceLookup
from casttrail.providers import comiconomicon, rostercon
from casttrail.providers.base import LinkTarget
from casttrail.watching.models import Watch

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


# A page found by name is trusted for this long before the name is looked up again.
# The lookup also asks whether a second person has the name, which can change, so a
# page is not trusted for ever. A page set by hand is trusted until it is removed.
RECHECK_AFTER = timedelta(days=30)


@dataclass(frozen=True)
class LinkSource:
    """A site a watched person can be linked to a page on.

    A new source is one more of these: its name in the database, how it is shown, an
    example address for the form, and the parser that checks a pasted address.
    """

    provider: str
    label: str
    example: str
    parse: Callable[[str], LinkTarget | None]
    # The address of the page with this key, or None if the key is not a plain one.
    page_url: Callable[[str], str | None]


def _parse_rostercon(text: str) -> LinkTarget | None:
    page = rostercon.parse_person_link(text)
    return LinkTarget(key=page.slug, url=page.url) if page else None


ROSTERCON = LinkSource(
    provider=rostercon.PROVIDER_ID,
    label="Rostercon",
    example="https://www.rostercon.com/en/people/tom-wilson-en",
    parse=_parse_rostercon,
    page_url=rostercon.person_url,
)
COMICONOMICON = LinkSource(
    provider=comiconomicon.PROVIDER_ID,
    label="Comiconomicon",
    example="https://comiconomicon.com/guest/518/Andrew_Jackson",
    parse=comiconomicon.parse_guest_link,
    page_url=comiconomicon.guest_url,
)
SOURCES = (ROSTERCON, COMICONOMICON)


def source_by_provider(provider: str) -> LinkSource | None:
    """Find a source by its name in the database.

    Args:
        provider: The provider id, such as ``rostercon``.

    Returns:
        The source, or ``None`` if there is no such source.
    """
    return next((s for s in SOURCES if s.provider == provider), None)


def page_url_for(provider: str, key: str) -> str | None:
    """Give the address of a guest's page on a source, to show as a link.

    Args:
        provider: The source's provider id.
        key: The source's own id for the guest.

    Returns:
        The address, or ``None`` if the source is not one we link to or the key is not
        a plain one for it.
    """
    source = source_by_provider(provider)
    return source.page_url(key) if source is not None else None


class InvalidLinkError(ValueError):
    """What was pasted is not the address of a page on the source."""


class UnknownPersonError(LookupError):
    """There is no such person."""


@dataclass(frozen=True)
class SourceLink:
    """A watched person and what is known about their page on one source.

    ``state`` is ``manual`` (a person set the link), ``found`` (the name matched one
    page), ``not_found``, ``ambiguous``, or ``unknown`` when nothing has looked yet.
    """

    person_id: uuid.UUID
    name: str
    state: str
    key: str | None
    url: str | None
    looked_up_at: datetime | None


@dataclass(frozen=True)
class KnownPage:
    """A person's page on a source that need not be looked up by name."""

    page: LinkTarget
    manual: bool


class SourceLinks:
    """Links watched people to their pages on one source, by hand or by a lookup.

    A link set by hand always wins: a refresh uses it instead of guessing the page from
    the name, and nothing the refresh learns replaces or removes it.
    """

    def __init__(self, session: AsyncSession, source: LinkSource) -> None:
        """Create the service.

        Args:
            session: The database session.
            source: The site the links are on.
        """
        self._session = session
        self._source = source

    async def watched(self) -> list[SourceLink]:
        """List every watched person with the state of their link on this source.

        Returns:
            One row per person anyone watches, ordered by name.
        """
        rows = await self._session.execute(
            select(Person.id, Person.name, SourceLookup)
            .join(Watch, Watch.person_id == Person.id)
            .outerjoin(
                SourceLookup,
                (SourceLookup.person_id == Person.id)
                & (SourceLookup.provider == self._source.provider),
            )
            .distinct()
            .order_by(Person.name, Person.id)
        )
        return [_link(person_id, name, lookup) for person_id, name, lookup in rows]

    async def known(self, person_id: uuid.UUID) -> KnownPage | None:
        """Get a person's page if it is known well enough not to look the name up.

        A page set by hand is always known. A page found by name is known for
        ``RECHECK_AFTER``. A person who was not found, or has several pages, is never
        known: they are looked up again each time.

        Args:
            person_id: The person.

        Returns:
            The page, or ``None`` if the name must be looked up.
        """
        lookup = await self._stored(person_id)
        if lookup is None or lookup.outcome != "found":
            return None
        if not lookup.manual and utc_now() - lookup.looked_up_at >= RECHECK_AFTER:
            return None
        assert lookup.source_key is not None
        assert lookup.source_url is not None
        page = LinkTarget(key=lookup.source_key, url=lookup.source_url)
        return KnownPage(page, lookup.manual)

    async def manual(self, person_id: uuid.UUID) -> LinkTarget | None:
        """Get the page a person set by hand for someone.

        Args:
            person_id: The person.

        Returns:
            The page, or ``None`` if nobody set one.
        """
        known = await self.known(person_id)
        return known.page if known is not None and known.manual else None

    async def link(self, person_id: uuid.UUID, text: str) -> LinkTarget:
        """Set a person's page by hand, replacing any earlier answer.

        Args:
            person_id: The person.
            text: The pasted address of their page on this source.

        Returns:
            The page that was stored.

        Raises:
            InvalidLinkError: The text is not a page address on this source.
            UnknownPersonError: There is no such person.
        """
        page = self._source.parse(text)
        if page is None:
            msg = f"That is not the address of a {self._source.label} page."
            raise InvalidLinkError(msg)
        if await self._session.get(Person, person_id) is None:
            raise UnknownPersonError(str(person_id))
        lookup = await self._stored(person_id) or SourceLookup(
            provider=self._source.provider, person_id=person_id
        )
        lookup.outcome = "found"
        lookup.source_key, lookup.source_url = page.key, page.url
        lookup.manual = True
        lookup.looked_up_at = utc_now()
        self._session.add(lookup)
        await self._session.commit()
        logger.info(
            "source_link_set",
            source=self._source.provider,
            person_id=str(person_id),
            key=page.key,
        )
        return page

    async def unlink(self, person_id: uuid.UUID) -> bool:
        """Remove a link set by hand. The next refresh finds the person by name again.

        Args:
            person_id: The person.

        Returns:
            True if a link was removed, False if there was none by hand.
        """
        result = await self._session.execute(
            delete(SourceLookup).where(
                SourceLookup.provider == self._source.provider,
                SourceLookup.person_id == person_id,
                SourceLookup.manual.is_(True),
            )
        )
        await self._session.commit()
        removed = bool(result.rowcount)  # type: ignore[attr-defined]
        if removed:
            logger.info(
                "source_link_removed",
                source=self._source.provider,
                person_id=str(person_id),
            )
        return removed

    async def remember(
        self, person_id: uuid.UUID, outcome: str, page: LinkTarget | None = None
    ) -> None:
        """Note what a lookup by name found, unless a person has set the link.

        Args:
            person_id: The person.
            outcome: ``found``, ``not_found`` or ``ambiguous``.
            page: The page, when ``outcome`` is ``found``.
        """
        lookup = await self._stored(person_id)
        if lookup is not None and lookup.manual:
            return
        lookup = lookup or SourceLookup(
            provider=self._source.provider, person_id=person_id
        )
        lookup.outcome = outcome
        lookup.source_key = page.key if page else None
        lookup.source_url = page.url if page else None
        lookup.looked_up_at = utc_now()
        self._session.add(lookup)
        await self._session.commit()

    async def _stored(self, person_id: uuid.UUID) -> SourceLookup | None:
        return await self._session.scalar(
            select(SourceLookup).where(
                SourceLookup.provider == self._source.provider,
                SourceLookup.person_id == person_id,
            )
        )


def _link(person_id: uuid.UUID, name: str, lookup: SourceLookup | None) -> SourceLink:
    if lookup is None:
        return SourceLink(person_id, name, "unknown", None, None, None)
    state = "manual" if lookup.manual else lookup.outcome
    return SourceLink(
        person_id,
        name,
        state,
        lookup.source_key,
        lookup.source_url,
        lookup.looked_up_at,
    )
