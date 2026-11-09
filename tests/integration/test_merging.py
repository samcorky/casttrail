import uuid
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from casttrail.catalogue.models import Person
from casttrail.discovery.merging import MergeService
from casttrail.discovery.models import (
    Appearance,
    AppearanceSource,
    Convention,
    ConventionMerge,
    ConventionSource,
)
from casttrail.discovery.service import DiscoveryService
from casttrail.identity.models import ExternalIdentity
from casttrail.providers.base import ConventionInfo, GuestObservation, ListingResult

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from time_machine import TimeMachineFixture

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def _info(key: str = "a", name: str = "Some Con", **changes: object) -> ConventionInfo:
    values: dict[str, object] = {
        "source_key": key,
        "name": name,
        "source_url": f"https://example.test/{key}",
        "start_date": date(2027, 4, 17),
        "end_date": date(2027, 4, 18),
    }
    return ConventionInfo(**{**values, **changes})


def _guests(*keys: str, names: dict[str, str] | None = None) -> ListingResult:
    names = names or {}
    return ListingResult(
        items=[
            GuestObservation(
                source_key=k,
                name=names.get(k, k.title()),
                source_url=f"https://example.test/g/{k}",
            )
            for k in keys
        ],
        complete=False,
    )


@pytest.fixture(autouse=True)
def _start_the_clock(time_machine: TimeMachineFixture) -> None:
    """Start the clock at a known moment, then let it run, so later things are later."""
    time_machine.move_to(NOW, tick=True)


async def _count(session: AsyncSession, model: type) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def _pair(
    session: AsyncSession, provider: str = "rostercon", other: str = "comiconomicon"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Two alike events from two sources, left as a suggested pair: (pair, one, two)."""
    service = DiscoveryService(session)
    one = await service.ingest(provider, _info("a", "Some Con"), _guests("amy", "bob"))
    two = await service.ingest(
        other, _info("1", "Some Con Lincoln"), _guests("amy", "cat")
    )
    pair = await session.scalar(select(ConventionMerge.id))
    assert pair is not None
    return pair, one.convention_id, two.convention_id


@pytest.fixture
def service(db_session: AsyncSession) -> MergeService:
    return MergeService(db_session)


# --- listing ----------------------------------------------------------------------


async def test_a_suggested_pair_shows_both_sides_with_sources_and_guest_counts(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, one, two = await _pair(db_session)

    (view,) = await service.pairs("suggested")

    assert view.pair_id == pair
    assert view.reason == "Alike names on overlapping days."
    sides = {
        view.first.convention.id: view.first,
        view.second.convention.id: view.second,
    }
    assert sides[one].sources == ["Rostercon"]
    assert sides[two].sources == ["Comiconomicon"]
    assert (sides[one].guests, sides[two].guests) == (2, 2)
    assert await service.pairs("rejected") == []


async def test_pairs_are_listed_oldest_first(
    db_session: AsyncSession,
    service: MergeService,
    time_machine: TimeMachineFixture,
) -> None:
    await _pair(db_session)
    for number in range(5):
        time_machine.move_to(NOW + timedelta(days=1 + number))
        await DiscoveryService(db_session).ingest(
            "comiconomicon",
            _info(f"x{number}", f"Some Con Hall {number}"),
            _guests("dan"),
        )

    views = await service.pairs("suggested")

    stored = list(
        await db_session.scalars(
            select(ConventionMerge.id).order_by(ConventionMerge.created_at)
        )
    )
    assert len(views) == 6
    assert [v.pair_id for v in views] == stored


# --- merging ----------------------------------------------------------------------


async def test_merging_keeps_the_chosen_event_and_brings_everything_across(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, one, two = await _pair(db_session)

    assert await service.merge(pair, one) == "done"

    assert await db_session.get(Convention, two) is None
    kept = await db_session.get(Convention, one)
    assert kept is not None
    assert kept.name == "Some Con"
    providers = sorted(
        await db_session.scalars(
            select(ConventionSource.provider).where(
                ConventionSource.convention_id == one
            )
        )
    )
    assert providers == ["comiconomicon", "rostercon"]
    assert await _count(db_session, ConventionMerge) == 0


async def test_either_event_can_be_the_one_kept(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, one, two = await _pair(db_session)

    assert await service.merge(pair, two) == "done"

    assert await db_session.get(Convention, one) is None
    kept = await db_session.get(Convention, two)
    assert kept is not None
    assert kept.name == "Some Con Lincoln"


async def test_unknown_details_are_filled_in_and_known_ones_are_kept(
    db_session: AsyncSession, service: MergeService
) -> None:
    clock = DiscoveryService(db_session)
    await clock.ingest(
        "rostercon",
        _info("a", "Some Con", venue="Real Hall", city=None, organizer=None),
        _guests("amy"),
    )
    other = await clock.ingest(
        "comiconomicon",
        _info(
            "1", "Some Con Lincoln", venue="Other Text", city="Lincoln", organizer="Org"
        ),
        _guests("amy"),
    )
    pair = await db_session.scalar(select(ConventionMerge.id))
    assert pair is not None
    first = await db_session.scalar(
        select(ConventionSource.convention_id).where(
            ConventionSource.provider == "rostercon"
        )
    )
    assert first is not None

    await service.merge(pair, first)

    kept = await db_session.get(Convention, first)
    assert kept is not None
    assert other.convention_id != first
    assert (kept.venue, kept.city, kept.organizer) == ("Real Hall", "Lincoln", "Org")


async def test_the_same_guest_at_both_events_becomes_one_row_with_both_sources(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, one, _ = await _pair(db_session)
    # amy is listed by both sources, as two separate identities.
    assert await _count(db_session, Appearance) == 4

    await service.merge(pair, one)

    rows = list(
        await db_session.execute(
            select(ExternalIdentity.display_name, AppearanceSource.provider)
            .join(Appearance, Appearance.external_identity_id == ExternalIdentity.id)
            .join(AppearanceSource, AppearanceSource.appearance_id == Appearance.id)
            .where(Appearance.convention_id == one)
            .order_by(ExternalIdentity.display_name, AppearanceSource.provider)
        )
    )
    assert rows == [
        ("Amy", "comiconomicon"),
        ("Amy", "rostercon"),
        ("Bob", "rostercon"),
        ("Cat", "comiconomicon"),
    ]
    assert await _count(db_session, Appearance) == 4


async def _shared_guest(
    session: AsyncSession, one: uuid.UUID, two: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, Person]:
    """One identity listed at both events, by a different source at each.

    Today each source makes its own identities, so this shape only arises once
    identities are shared across sources. The merge must still cope with it, because
    the database allows one appearance per identity and event.

    Returns the kept-side appearance id, the dropped-side appearance id and the person
    the dropped side was linked to.
    """
    person = Person(id=uuid.uuid4(), name="Amy")
    identity = ExternalIdentity(
        id=uuid.uuid4(), provider="tmdb", external_id="1", display_name="Amy"
    )
    session.add_all([person, identity])
    await session.flush()
    here = Appearance(
        id=uuid.uuid4(), convention_id=one, external_identity_id=identity.id
    )
    there = Appearance(
        id=uuid.uuid4(),
        convention_id=two,
        external_identity_id=identity.id,
        person_id=person.id,
    )
    session.add_all([here, there])
    await session.flush()
    for appearance, provider in ((here, "rostercon"), (there, "comiconomicon")):
        session.add(
            AppearanceSource(
                id=uuid.uuid4(),
                appearance_id=appearance.id,
                provider=provider,
                source_url=f"https://example.test/{provider}",
            )
        )
    await session.commit()
    return here.id, there.id, person


async def test_one_identity_at_both_events_folds_into_one_appearance(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, one, two = await _pair(db_session)
    here, there, person = await _shared_guest(db_session, one, two)
    person_id = person.id

    assert await service.merge(pair, one) == "done"

    folded = await db_session.get(Appearance, here)
    assert folded is not None
    assert await db_session.get(Appearance, there) is None
    # The person link made on the dropped event's guest carries over to the kept one.
    assert folded.person_id == person_id
    providers = sorted(
        await db_session.scalars(
            select(AppearanceSource.provider).where(
                AppearanceSource.appearance_id == here
            )
        )
    )
    assert providers == ["comiconomicon", "rostercon"]


async def test_the_kept_guests_own_link_beats_the_dropped_ones(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, one, two = await _pair(db_session)
    here, _, _ = await _shared_guest(db_session, one, two)
    mine = Person(id=uuid.uuid4(), name="Amy Kept")
    db_session.add(mine)
    await db_session.flush()
    kept = await db_session.get(Appearance, here)
    assert kept is not None
    kept.person_id = mine.id
    await db_session.commit()
    mine_id = mine.id

    await service.merge(pair, one)

    await db_session.refresh(kept)
    assert kept.person_id == mine_id


async def test_a_duplicate_source_line_on_a_shared_guest_keeps_the_kept_ones(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, one, two = await _pair(db_session)
    here, there, _ = await _shared_guest(db_session, one, two)
    # The dropped guest also carries a "rostercon" line; the kept guest has its own.
    db_session.add(
        AppearanceSource(
            id=uuid.uuid4(),
            appearance_id=there,
            provider="rostercon",
            source_url="https://example.test/dropped-line",
        )
    )
    await db_session.commit()

    await service.merge(pair, one)

    urls = list(
        await db_session.scalars(
            select(AppearanceSource.source_url).where(
                AppearanceSource.appearance_id == here,
                AppearanceSource.provider == "rostercon",
            )
        )
    )
    assert urls == ["https://example.test/rostercon"]


async def test_a_pairs_other_pairs_move_onto_the_kept_event(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, one, two = await _pair(db_session)
    third = DiscoveryService(db_session)
    other = await third.ingest(
        "comiconomicon", _info("2", "Some Con Hall"), _guests("dan")
    )
    assert await _count(db_session, ConventionMerge) == 2

    # Joining one with two should leave the Hall event suggested against what remains.
    await service.merge(pair, one)

    rows = list(await db_session.scalars(select(ConventionMerge)))
    assert len(rows) == 1
    assert {rows[0].first_id, rows[0].second_id} == {one, other.convention_id}
    assert rows[0].first_id < rows[0].second_id or str(rows[0].first_id) < str(
        rows[0].second_id
    )
    assert two not in {rows[0].first_id, rows[0].second_id}


async def test_a_rejection_wins_when_two_pairs_fold_into_one(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, one, two = await _pair(db_session)
    third = DiscoveryService(db_session)
    other = (
        await third.ingest("comiconomicon", _info("2", "Some Con Hall"), _guests("dan"))
    ).convention_id
    # "one" and "other" are suggested; "two" and "other" were already judged different.
    first, second = sorted((two, other), key=str)
    db_session.add(
        ConventionMerge(
            id=uuid.uuid4(),
            first_id=first,
            second_id=second,
            status="rejected",
            reason="r",
            created_at=NOW,
            decided_at=NOW,
        )
    )
    await db_session.commit()

    await service.merge(pair, one)

    rows = list(await db_session.scalars(select(ConventionMerge)))
    assert [(r.status, {r.first_id, r.second_id}) for r in rows] == [
        ("rejected", {one, other})
    ]


async def test_a_suggestion_does_not_override_an_existing_rejection_when_folding(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, one, two = await _pair(db_session)
    third = DiscoveryService(db_session)
    other = (
        await third.ingest("comiconomicon", _info("2", "Some Con Hall"), _guests("dan"))
    ).convention_id
    # "one" and "other" were judged different; "two" and "other" are only suggested.
    rows = list(await db_session.scalars(select(ConventionMerge)))
    mine = next(r for r in rows if {r.first_id, r.second_id} == {one, other})
    mine.status = "rejected"
    first, second = sorted((two, other), key=str)
    db_session.add(
        ConventionMerge(
            id=uuid.uuid4(),
            first_id=first,
            second_id=second,
            status="suggested",
            reason="r",
            created_at=NOW,
        )
    )
    await db_session.commit()

    await service.merge(pair, one)

    (left,) = list(await db_session.scalars(select(ConventionMerge)))
    assert left.status == "rejected"


async def test_the_kept_event_is_marked_as_changed(
    db_session: AsyncSession,
    service: MergeService,
    time_machine: TimeMachineFixture,
) -> None:
    pair, one, _ = await _pair(db_session)
    kept = await db_session.get(Convention, one)
    assert kept is not None
    before = kept.updated_at
    time_machine.move_to(NOW + timedelta(days=5))

    await service.merge(pair, one)

    await db_session.refresh(kept)
    assert kept.updated_at >= NOW + timedelta(days=5)
    assert kept.updated_at > before


async def test_an_event_that_is_not_in_the_pair_cannot_be_kept(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, one, two = await _pair(db_session)
    stranger = (
        await DiscoveryService(db_session).ingest(
            "comiconomicon",
            _info(
                "9",
                "Unrelated Event",
                start_date=date(2028, 1, 1),
                end_date=date(2028, 1, 2),
            ),
            _guests("zed"),
        )
    ).convention_id

    assert await service.merge(pair, stranger) == "missing"

    assert await db_session.get(Convention, one) is not None
    assert await db_session.get(Convention, two) is not None
    assert await db_session.get(Convention, stranger) is not None
    assert await _count(db_session, Convention) == 3


async def test_a_dropped_source_ingests_into_the_kept_event_afterwards(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, one, _ = await _pair(db_session)
    await service.merge(pair, one)

    again = await DiscoveryService(db_session).ingest(
        "comiconomicon", _info("1", "Some Con Lincoln"), _guests("amy", "cat")
    )

    assert again.convention_id == one
    assert again.convention_created is False
    assert again.merge_suggestions == 0
    assert await _count(db_session, Convention) == 1


async def test_two_events_one_source_lists_are_never_joined(
    db_session: AsyncSession, service: MergeService
) -> None:
    clock = DiscoveryService(db_session)
    await clock.ingest("rostercon", _info("a", "Some Con"), _guests("amy"))
    await clock.ingest("rostercon", _info("b", "Some Con"), _guests("bob"))
    await clock.ingest("comiconomicon", _info("1", "Some Con"), _guests("cat"))
    pairs = list(await db_session.scalars(select(ConventionMerge)))
    assert len(pairs) == 2
    # Join the Comiconomicon event into the first Rostercon one.
    comicon = await db_session.scalar(
        select(ConventionSource.convention_id).where(
            ConventionSource.provider == "comiconomicon"
        )
    )
    first = await db_session.scalar(
        select(ConventionSource.convention_id).where(ConventionSource.source_key == "a")
    )
    assert comicon is not None
    assert first is not None
    target = next(p for p in pairs if {p.first_id, p.second_id} == {comicon, first})
    assert await service.merge(target.id, first) == "done"
    # Now the other Rostercon event is suggested against the merged one: a source cannot
    # be joined with itself.
    (left,) = list(await db_session.scalars(select(ConventionMerge)))
    before = await _count(db_session, Convention)

    assert await service.merge(left.id, first) == "same-source"

    assert await _count(db_session, Convention) == before
    await db_session.refresh(left)
    assert left.status == "suggested"


async def test_nothing_changes_when_a_merge_cannot_be_done(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, one, two = await _pair(db_session)
    nobody = uuid.uuid4()

    assert await service.merge(nobody, one) == "missing"
    assert await service.merge(pair, nobody) == "missing"

    assert await _count(db_session, Convention) == 2
    assert await _count(db_session, ConventionMerge) == 1
    assert two is not None


async def test_a_rejected_pair_cannot_be_merged(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, one, _ = await _pair(db_session)
    await service.reject(pair)

    assert await service.merge(pair, one) == "missing"
    assert await _count(db_session, Convention) == 2


async def test_a_failure_part_way_leaves_everything_as_it_was(
    db_session: AsyncSession,
    service: MergeService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair, one, two = await _pair(db_session)

    async def boom(*args: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_repoint_pairs", boom)

    with pytest.raises(RuntimeError, match="boom"):
        await service.merge(pair, one)

    monkeypatch.undo()
    assert await db_session.get(Convention, one) is not None
    assert await db_session.get(Convention, two) is not None
    sources = list(
        await db_session.scalars(
            select(ConventionSource.convention_id).order_by(ConventionSource.provider)
        )
    )
    assert sorted(sources) == sorted([one, two])
    assert await _count(db_session, Appearance) == 4


# --- rejecting and reconsidering --------------------------------------------------


async def test_rejecting_marks_the_pair_and_keeps_both_events(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, one, two = await _pair(db_session)

    assert await service.reject(pair)

    row = await db_session.get(ConventionMerge, pair)
    assert row is not None
    assert row.status == "rejected"
    assert row.decided_at is not None
    assert await db_session.get(Convention, one) is not None
    assert await db_session.get(Convention, two) is not None
    assert await service.pairs("suggested") == []
    assert len(await service.pairs("rejected")) == 1


async def test_a_rejected_pair_is_not_suggested_again_by_a_later_refresh(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, _, _ = await _pair(db_session)
    await service.reject(pair)

    result = await DiscoveryService(db_session).ingest(
        "comiconomicon", _info("1", "Some Con Lincoln"), _guests("amy", "cat")
    )

    assert result.merge_suggestions == 0
    assert await service.pairs("suggested") == []


async def test_rejecting_twice_or_an_unknown_pair_does_nothing(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, _, _ = await _pair(db_session)

    assert await service.reject(pair)
    assert not await service.reject(pair)
    assert not await service.reject(uuid.uuid4())


async def test_a_rejection_can_be_taken_back(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, _, _ = await _pair(db_session)
    await service.reject(pair)

    assert await service.reconsider(pair)

    row = await db_session.get(ConventionMerge, pair)
    assert row is not None
    assert (row.status, row.decided_at) == ("suggested", None)
    assert len(await service.pairs("suggested")) == 1


async def test_only_a_rejected_pair_can_be_reconsidered(
    db_session: AsyncSession, service: MergeService
) -> None:
    pair, _, _ = await _pair(db_session)

    assert not await service.reconsider(pair)
    assert not await service.reconsider(uuid.uuid4())
