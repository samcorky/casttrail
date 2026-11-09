import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import structlog
from sqlalchemy import delete, select, update

from casttrail.db import utc_now
from casttrail.discovery.browse import guest_counts, source_label
from casttrail.discovery.models import (
    Appearance,
    AppearanceSource,
    Convention,
    ConventionMerge,
    ConventionSource,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

Outcome = Literal["done", "missing", "same-source"]

# Details copied across when the kept convention does not know them.
_FILL = (
    "start_date",
    "end_date",
    "venue",
    "city",
    "region",
    "country",
    "organizer",
    "status",
)


@dataclass(frozen=True)
class Side:
    """One of the two conventions in a suggested pair."""

    convention: Convention
    sources: list[str]
    guests: int


@dataclass(frozen=True)
class PairView:
    """A suggested or rejected pair, with both sides to compare."""

    pair_id: uuid.UUID
    reason: str
    first: Side
    second: Side


class MergeService:
    """Decides whether two conventions are the same event, and joins them if so.

    A person chooses which convention to keep. The other's sources move across, its
    guests are folded into the kept convention's (the same guest at both becomes one),
    anything the kept convention does not know is filled in from it, and it is deleted.
    All of that happens in one transaction. Two conventions that the same source lists
    are never joined: a source's two events are two events.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Create the service.

        Args:
            session: The database session to work in.
        """
        self._session = session

    async def pairs(self, status: Literal["suggested", "rejected"]) -> list[PairView]:
        """List pairs waiting for a decision, or ones decided as different.

        Args:
            status: Which pairs to list.

        Returns:
            The pairs, oldest first, each with both sides' details.
        """
        rows = list(
            await self._session.scalars(
                select(ConventionMerge)
                .where(ConventionMerge.status == status)
                .order_by(ConventionMerge.created_at, ConventionMerge.id)
            )
        )
        ids = [i for row in rows for i in (row.first_id, row.second_id)]
        counts = await guest_counts(self._session, ids)
        views: list[PairView] = []
        for row in rows:
            first = await self._side(row.first_id, counts)
            second = await self._side(row.second_id, counts)
            views.append(PairView(row.id, row.reason, first, second))
        return views

    async def _side(
        self, convention_id: uuid.UUID, counts: dict[uuid.UUID, int]
    ) -> Side:
        convention = await self._session.get_one(Convention, convention_id)
        providers = await self._session.scalars(
            select(ConventionSource.provider)
            .where(ConventionSource.convention_id == convention_id)
            .order_by(ConventionSource.provider)
        )
        labels = [source_label(p) for p in providers]
        return Side(convention, labels, counts.get(convention_id, 0))

    async def merge(self, pair_id: uuid.UUID, keep_id: uuid.UUID) -> Outcome:
        """Join the two conventions of a suggested pair, keeping one.

        Args:
            pair_id: The suggested pair.
            keep_id: Which of its two conventions to keep.

        Returns:
            ``done``; ``missing`` if there is no such suggested pair or ``keep_id`` is
            not one of its conventions; or ``same-source`` if one source lists both.
        """
        try:
            outcome = await self._merge(pair_id, keep_id)
            if outcome == "done":
                await self._session.commit()
            else:
                await self._session.rollback()
        except BaseException:
            await self._session.rollback()
            logger.exception("merge_failed", pair=str(pair_id), keep=str(keep_id))
            raise
        if outcome != "done":
            logger.info("merge_refused", pair=str(pair_id), reason=outcome)
        return outcome

    async def reject(self, pair_id: uuid.UUID) -> bool:
        """Say a suggested pair are different events. They are never suggested again.

        Args:
            pair_id: The suggested pair.

        Returns:
            ``False`` if there is no such suggested pair.
        """
        return await self._decide(pair_id, "suggested", "rejected")

    async def reconsider(self, pair_id: uuid.UUID) -> bool:
        """Take back a rejection, so the pair is suggested again.

        Args:
            pair_id: The rejected pair.

        Returns:
            ``False`` if there is no such rejected pair.
        """
        return await self._decide(pair_id, "rejected", "suggested")

    async def _decide(self, pair_id: uuid.UUID, was: str, becomes: str) -> bool:
        pair = await self._session.get(ConventionMerge, pair_id)
        if pair is None or pair.status != was:
            return False
        pair.status = becomes
        pair.decided_at = utc_now() if becomes == "rejected" else None
        await self._session.commit()
        return True

    async def _merge(self, pair_id: uuid.UUID, keep_id: uuid.UUID) -> Outcome:
        pair = await self._session.get(ConventionMerge, pair_id)
        if pair is None or pair.status != "suggested":
            return "missing"
        if keep_id not in (pair.first_id, pair.second_id):
            return "missing"
        drop_id = pair.second_id if keep_id == pair.first_id else pair.first_id
        # Both exist: the pair's foreign keys delete the pair with either convention.
        keep = await self._session.get_one(Convention, keep_id)
        drop = await self._session.get_one(Convention, drop_id)
        if await self._share_a_source(keep_id, drop_id):
            return "same-source"
        await self._session.execute(
            update(ConventionSource)
            .where(ConventionSource.convention_id == drop_id)
            .values(convention_id=keep_id)
        )
        for name in _FILL:
            if getattr(keep, name) is None and getattr(drop, name) is not None:
                setattr(keep, name, getattr(drop, name))
        keep.updated_at = utc_now()
        await self._fold_guests(keep_id, drop_id)
        await self._repoint_pairs(pair.id, keep_id, drop_id)
        await self._session.flush()
        await self._session.execute(delete(Convention).where(Convention.id == drop_id))
        return "done"

    async def _share_a_source(self, one: uuid.UUID, other: uuid.UUID) -> bool:
        async def providers(convention_id: uuid.UUID) -> set[str]:
            rows = await self._session.scalars(
                select(ConventionSource.provider).where(
                    ConventionSource.convention_id == convention_id
                )
            )
            return set(rows)

        return bool(await providers(one) & await providers(other))

    async def _fold_guests(self, keep_id: uuid.UUID, drop_id: uuid.UUID) -> None:
        """Bring the dropped convention's guests across, one row per guest."""
        kept = {
            a.external_identity_id: a
            for a in await self._session.scalars(
                select(Appearance).where(Appearance.convention_id == keep_id)
            )
        }
        for appearance in list(
            await self._session.scalars(
                select(Appearance).where(Appearance.convention_id == drop_id)
            )
        ):
            target = kept.get(appearance.external_identity_id)
            if target is None:
                appearance.convention_id = keep_id
                kept[appearance.external_identity_id] = appearance
                continue
            await self._fold_lines(appearance, target)
            if target.person_id is None and appearance.person_id is not None:
                target.person_id = appearance.person_id
            await self._session.flush()
            await self._session.delete(appearance)

    async def _fold_lines(self, source: Appearance, target: Appearance) -> None:
        """Move a duplicate guest's source lines onto the guest it duplicates.

        A line from a source the target already has is dropped, so the target's wins.
        """
        have = set(
            await self._session.scalars(
                select(AppearanceSource.provider).where(
                    AppearanceSource.appearance_id == target.id
                )
            )
        )
        for line in list(
            await self._session.scalars(
                select(AppearanceSource).where(
                    AppearanceSource.appearance_id == source.id
                )
            )
        ):
            if line.provider not in have:
                line.appearance_id = target.id

    async def _repoint_pairs(
        self, decided_id: uuid.UUID, keep_id: uuid.UUID, drop_id: uuid.UUID
    ) -> None:
        """Move the dropped convention's other pairs onto the kept one.

        Where the kept convention already has a pair with the same other event, the
        two are folded into one, and a rejection wins over a suggestion.
        """
        others = list(
            await self._session.scalars(
                select(ConventionMerge).where(
                    ConventionMerge.id != decided_id,
                    (ConventionMerge.first_id == drop_id)
                    | (ConventionMerge.second_id == drop_id),
                )
            )
        )
        for row in others:
            other = row.second_id if row.first_id == drop_id else row.first_id
            first, second = sorted((keep_id, other), key=str)
            existing = await self._session.scalar(
                select(ConventionMerge).where(
                    ConventionMerge.first_id == first,
                    ConventionMerge.second_id == second,
                )
            )
            if existing is None:
                row.first_id, row.second_id = first, second
            else:
                if row.status == "rejected":
                    existing.status = "rejected"
                    existing.decided_at = row.decided_at
                await self._session.delete(row)
            await self._session.flush()
