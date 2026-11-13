import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import delete, select

from casttrail.catalogue.models import Person
from casttrail.db import utc_now
from casttrail.discovery.models import SourceLookup
from casttrail.providers.rostercon import PROVIDER_ID, PersonPage, parse_person_link
from casttrail.watching.models import Watch

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


class InvalidLinkError(ValueError):
    """What was pasted is not the address of a Rostercon person page."""


class UnknownPersonError(LookupError):
    """There is no such person."""


@dataclass(frozen=True)
class RosterconLink:
    """A watched person and what is known about their Rostercon page.

    ``state`` is ``manual`` (a person set the link), ``found`` (the name matched one
    page), ``not_found``, ``ambiguous``, or ``unknown`` when no refresh has looked yet.
    """

    person_id: uuid.UUID
    name: str
    state: str
    slug: str | None
    url: str | None
    looked_up_at: datetime | None


class RosterconLinks:
    """Links watched people to their Rostercon pages, by hand or by what a refresh saw.

    A link set by hand always wins: a refresh uses it instead of guessing the page from
    the name, and nothing the refresh learns replaces or removes it.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Create the service.

        Args:
            session: The database session.
        """
        self._session = session

    async def watched(self) -> list[RosterconLink]:
        """List every watched person with the state of their Rostercon link.

        Returns:
            One row per person anyone watches, ordered by name.
        """
        rows = await self._session.execute(
            select(Person.id, Person.name, SourceLookup)
            .join(Watch, Watch.person_id == Person.id)
            .outerjoin(
                SourceLookup,
                (SourceLookup.person_id == Person.id)
                & (SourceLookup.provider == PROVIDER_ID),
            )
            .distinct()
            .order_by(Person.name, Person.id)
        )
        return [_link(person_id, name, lookup) for person_id, name, lookup in rows]

    async def manual(self, person_id: uuid.UUID) -> PersonPage | None:
        """Get the page a person set by hand for someone.

        Args:
            person_id: The person.

        Returns:
            The page, or ``None`` if nobody set one.
        """
        lookup = await self._stored(person_id)
        if lookup is None or not lookup.manual:
            return None
        assert lookup.source_key is not None
        assert lookup.source_url is not None
        return PersonPage(slug=lookup.source_key, url=lookup.source_url)

    async def link(self, person_id: uuid.UUID, text: str) -> PersonPage:
        """Set a person's Rostercon page by hand, replacing any earlier answer.

        Args:
            person_id: The person.
            text: The pasted address of their Rostercon page.

        Returns:
            The page that was stored.

        Raises:
            InvalidLinkError: The text is not a Rostercon person page address.
            UnknownPersonError: There is no such person.
        """
        page = parse_person_link(text)
        if page is None:
            raise InvalidLinkError(
                "That is not the address of a Rostercon person page."
            )
        if await self._session.get(Person, person_id) is None:
            raise UnknownPersonError(str(person_id))
        lookup = await self._stored(person_id) or SourceLookup(
            provider=PROVIDER_ID, person_id=person_id
        )
        lookup.outcome = "found"
        lookup.source_key, lookup.source_url = page.slug, page.url
        lookup.manual = True
        lookup.looked_up_at = utc_now()
        self._session.add(lookup)
        await self._session.commit()
        logger.info("rostercon_link_set", person_id=str(person_id), slug=page.slug)
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
                SourceLookup.provider == PROVIDER_ID,
                SourceLookup.person_id == person_id,
                SourceLookup.manual.is_(True),
            )
        )
        await self._session.commit()
        removed = bool(result.rowcount)  # type: ignore[attr-defined]
        if removed:
            logger.info("rostercon_link_removed", person_id=str(person_id))
        return removed

    async def remember(
        self, person_id: uuid.UUID, outcome: str, page: PersonPage | None = None
    ) -> None:
        """Note what a refresh found by name, unless a person has set the link.

        Args:
            person_id: The person.
            outcome: ``found``, ``not_found`` or ``ambiguous``.
            page: The page, when ``outcome`` is ``found``.
        """
        lookup = await self._stored(person_id)
        if lookup is not None and lookup.manual:
            return
        lookup = lookup or SourceLookup(provider=PROVIDER_ID, person_id=person_id)
        lookup.outcome = outcome
        lookup.source_key = page.slug if page else None
        lookup.source_url = page.url if page else None
        lookup.looked_up_at = utc_now()
        self._session.add(lookup)
        await self._session.commit()

    async def _stored(self, person_id: uuid.UUID) -> SourceLookup | None:
        return await self._session.scalar(
            select(SourceLookup).where(
                SourceLookup.provider == PROVIDER_ID,
                SourceLookup.person_id == person_id,
            )
        )


def _link(
    person_id: uuid.UUID, name: str, lookup: SourceLookup | None
) -> RosterconLink:
    if lookup is None:
        return RosterconLink(person_id, name, "unknown", None, None, None)
    state = "manual" if lookup.manual else lookup.outcome
    return RosterconLink(
        person_id,
        name,
        state,
        lookup.source_key,
        lookup.source_url,
        lookup.looked_up_at,
    )
