import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from casttrail.catalogue.models import Person
from casttrail.db import utc_now
from casttrail.discovery.models import Appearance, AppearanceSource
from casttrail.identity.models import ExternalIdentity, IdentityDecision
from casttrail.matching.models import Match
from casttrail.matching.resolver import Candidate, Guest, resolve
from casttrail.watching.models import Watch

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

# (user, appearance, match_key) names one match.
_Key = tuple[uuid.UUID, uuid.UUID, str]


@dataclass(frozen=True)
class RematchResult:
    """What a rematch changed."""

    added: int
    updated: int
    removed: int


@dataclass(frozen=True)
class _Wanted:
    person_id: uuid.UUID
    watch_id: uuid.UUID
    kind: str
    reason: str


def match_key_for_person(person_id: uuid.UUID) -> str:
    """Name what a match is about, for a watched person.

    Args:
        person_id: The watched person.

    Returns:
        A stable, never-empty key.
    """
    return f"person:{person_id}"


class MatchService:
    """Works out which guests are people users watch.

    ``rematch`` recomputes everything from the current guests, watches and decisions and
    brings the stored matches into line, so running it again changes nothing. It never
    writes decisions or links; it only reads them.
    """

    def __init__(
        self, session: AsyncSession, *, now: Callable[[], datetime] = utc_now
    ) -> None:
        """Create the service.

        Args:
            session: The database session to work in.
            now: The clock, replaceable in tests.
        """
        self._session = session
        self._now = now

    async def rematch(self) -> RematchResult:
        """Bring every user's matches up to date, all or nothing.

        Returns:
            How many matches were added, changed and removed.
        """
        try:
            result = await self._rematch()
            await self._session.commit()
        except BaseException:
            await self._session.rollback()
            logger.exception("rematch_failed")
            raise
        logger.debug(
            "rematched",
            added=result.added,
            updated=result.updated,
            removed=result.removed,
        )
        return result

    async def _rematch(self) -> RematchResult:
        wanted = await self._wanted()
        existing = {
            (m.user_id, m.appearance_id, m.match_key): m
            for m in await self._session.scalars(select(Match))
        }
        now = self._now()
        added = updated = 0
        for key, want in wanted.items():
            match = existing.get(key)
            if match is None:
                user_id, appearance_id, match_key = key
                self._session.add(
                    Match(
                        user_id=user_id,
                        appearance_id=appearance_id,
                        person_id=want.person_id,
                        watch_id=want.watch_id,
                        match_key=match_key,
                        kind=want.kind,
                        reason=want.reason,
                        created_at=now,
                        updated_at=now,
                    )
                )
                added += 1
            elif (match.kind, match.reason, match.watch_id, match.person_id) != (
                want.kind,
                want.reason,
                want.watch_id,
                want.person_id,
            ):
                match.kind = want.kind
                match.reason = want.reason
                match.watch_id = want.watch_id
                match.person_id = want.person_id
                match.updated_at = now
                updated += 1
        stale = [m for key, m in existing.items() if key not in wanted]
        for match in stale:
            await self._session.delete(match)
        return RematchResult(added=added, updated=updated, removed=len(stale))

    async def _wanted(self) -> dict[_Key, _Wanted]:
        """Work out every match that should exist right now."""
        watched: dict[uuid.UUID, list[tuple[Candidate, uuid.UUID]]] = {}
        rows = await self._session.execute(
            select(Watch.user_id, Watch.id, Person.id, Person.name)
            .join(Person, Person.id == Watch.person_id)
            .order_by(Watch.created_at, Watch.id)
        )
        for user_id, watch_id, person_id, name in rows:
            watched.setdefault(user_id, []).append(
                (Candidate(person_id, name), watch_id)
            )
        if not watched:
            return {}

        rejected: dict[uuid.UUID, set[uuid.UUID]] = {}
        for identity_id, person_id in await self._session.execute(
            select(
                IdentityDecision.external_identity_id, IdentityDecision.person_id
            ).where(IdentityDecision.decision == "rejected")
        ):
            rejected.setdefault(identity_id, set()).add(person_id)

        guests = await self._session.execute(
            select(
                Appearance.id,
                ExternalIdentity.id,
                ExternalIdentity.display_name,
                ExternalIdentity.person_id,
            )
            .join(
                ExternalIdentity,
                ExternalIdentity.id == Appearance.external_identity_id,
            )
            .where(
                select(AppearanceSource.id)
                .where(
                    AppearanceSource.appearance_id == Appearance.id,
                    AppearanceSource.listed.is_(True),
                )
                .exists()
            )
        )
        wanted: dict[_Key, _Wanted] = {}
        for appearance_id, identity_id, guest_name, linked in guests:
            guest = Guest(
                name=guest_name,
                linked_person_id=linked,
                rejected_person_ids=frozenset(rejected.get(identity_id, ())),
            )
            for user_id, entries in watched.items():
                result = resolve(guest, [candidate for candidate, _ in entries])
                if result.person_id is None:
                    continue
                found_watch = next(
                    (w for c, w in entries if c.person_id == result.person_id), None
                )
                if found_watch is None:
                    # Linked to someone this user does not watch: not theirs to see.
                    continue
                kind = "possible" if result.kind == "possible" else "confirmed"
                wanted[
                    (user_id, appearance_id, match_key_for_person(result.person_id))
                ] = _Wanted(result.person_id, found_watch, kind, result.reason)
        return wanted
