from typing import TYPE_CHECKING

from sqlalchemy import delete, select, update

from casttrail.catalogue.models import Person
from casttrail.db import utc_now
from casttrail.discovery.models import Appearance
from casttrail.identity.models import ExternalIdentity, IdentityDecision

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


class DecisionService:
    """Records what people decide about who a source's guest is.

    Decisions are made by a person and are never changed by a sync. Confirming links
    the guest, and every appearance of it, to the person; rejecting stops that person
    being offered for the guest again. Each method returns ``False`` when the guest or
    the person does not exist, and changes nothing.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Create the service.

        Args:
            session: The database session to work in.
        """
        self._session = session

    async def confirm(
        self, identity_id: uuid.UUID, person_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        """Say the guest is this person, replacing any earlier link.

        Args:
            identity_id: The guest.
            person_id: The person they are.
            user_id: Who decided.

        Returns:
            ``False`` if the guest or person does not exist.
        """
        identity = await self._both(identity_id, person_id)
        if identity is None:
            return False
        await self._session.execute(
            delete(IdentityDecision).where(
                IdentityDecision.external_identity_id == identity_id,
                (IdentityDecision.decision == "confirmed")
                | (IdentityDecision.person_id == person_id),
            )
        )
        self._session.add(self._row(identity_id, person_id, "confirmed", user_id))
        identity.person_id = person_id
        await self._set_appearances(identity_id, person_id)
        await self._session.commit()
        return True

    async def reject(
        self, identity_id: uuid.UUID, person_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        """Say the guest is not this person. If they were linked, the link is removed.

        Args:
            identity_id: The guest.
            person_id: The person they are not.
            user_id: Who decided.

        Returns:
            ``False`` if the guest or person does not exist.
        """
        identity = await self._both(identity_id, person_id)
        if identity is None:
            return False
        await self._session.execute(
            delete(IdentityDecision).where(
                IdentityDecision.external_identity_id == identity_id,
                IdentityDecision.person_id == person_id,
            )
        )
        self._session.add(self._row(identity_id, person_id, "rejected", user_id))
        if identity.person_id == person_id:
            identity.person_id = None
            await self._set_appearances(identity_id, None)
        await self._session.commit()
        return True

    async def unlink(self, identity_id: uuid.UUID) -> bool:
        """Remove the link between a guest and a person, without rejecting anyone.

        Args:
            identity_id: The guest.

        Returns:
            ``False`` if the guest does not exist.
        """
        identity = await self._session.get(ExternalIdentity, identity_id)
        if identity is None:
            return False
        await self._session.execute(
            delete(IdentityDecision).where(
                IdentityDecision.external_identity_id == identity_id,
                IdentityDecision.decision == "confirmed",
            )
        )
        identity.person_id = None
        await self._set_appearances(identity_id, None)
        await self._session.commit()
        return True

    async def forget_rejection(
        self, identity_id: uuid.UUID, person_id: uuid.UUID
    ) -> bool:
        """Take back a rejection, so the person can be offered for the guest again.

        Args:
            identity_id: The guest.
            person_id: The person who had been rejected.

        Returns:
            Whether there was a rejection to take back.
        """
        result = await self._session.execute(
            delete(IdentityDecision).where(
                IdentityDecision.external_identity_id == identity_id,
                IdentityDecision.person_id == person_id,
                IdentityDecision.decision == "rejected",
            )
        )
        await self._session.commit()
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def state(
        self, identity_id: uuid.UUID
    ) -> tuple[uuid.UUID | None, frozenset[uuid.UUID]]:
        """Read what is decided about a guest.

        Args:
            identity_id: The guest.

        Returns:
            The person they were confirmed as, if any, and the people rejected.
        """
        rows = await self._session.execute(
            select(IdentityDecision.person_id, IdentityDecision.decision).where(
                IdentityDecision.external_identity_id == identity_id
            )
        )
        linked: uuid.UUID | None = None
        rejected: set[uuid.UUID] = set()
        for person_id, decision in rows:
            if decision == "confirmed":
                linked = person_id
            else:
                rejected.add(person_id)
        return linked, frozenset(rejected)

    async def _both(
        self, identity_id: uuid.UUID, person_id: uuid.UUID
    ) -> ExternalIdentity | None:
        identity = await self._session.get(ExternalIdentity, identity_id)
        if identity is None or await self._session.get(Person, person_id) is None:
            return None
        return identity

    async def _set_appearances(
        self, identity_id: uuid.UUID, person_id: uuid.UUID | None
    ) -> None:
        await self._session.execute(
            update(Appearance)
            .where(Appearance.external_identity_id == identity_id)
            .values(person_id=person_id)
        )

    @staticmethod
    def _row(
        identity_id: uuid.UUID, person_id: uuid.UUID, decision: str, user_id: uuid.UUID
    ) -> IdentityDecision:
        return IdentityDecision(
            external_identity_id=identity_id,
            person_id=person_id,
            decision=decision,
            decided_by=user_id,
            decided_at=utc_now(),
        )
