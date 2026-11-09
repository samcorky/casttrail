import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from casttrail.auth.service import AuthService
from casttrail.catalogue.models import Person
from casttrail.discovery.models import Appearance, Convention
from casttrail.discovery.service import DiscoveryService
from casttrail.identity.decisions import DecisionService
from casttrail.identity.models import ExternalIdentity, IdentityDecision
from casttrail.matching.resolver import Candidate, Guest, resolve
from casttrail.providers.base import ConventionInfo, GuestObservation, ListingResult

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.auth.models import User

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 9, tzinfo=UTC)


class World:
    def __init__(
        self,
        user: User,
        paul: Person,
        twin: Person,
        guest: ExternalIdentity,
        appearance: Appearance,
    ) -> None:
        self.user = user
        self.paul = paul
        self.twin = twin
        self.guest = guest
        self.appearance = appearance


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    auth = AuthService(db_session, session_ttl=timedelta(hours=1))
    user = await auth.create_user(
        email="a@example.com", display_name="A", password="correct horse battery"
    )
    paul = Person(id=uuid.uuid4(), name="Paul McGann")
    twin = Person(id=uuid.uuid4(), name="Paul McGann")
    con = Convention(id=uuid.uuid4(), name="C", created_at=NOW, updated_at=NOW)
    guest = ExternalIdentity(
        id=uuid.uuid4(),
        provider="rostercon",
        external_id="paul-mcgann-en",
        display_name="Paul McGann",
    )
    db_session.add_all([paul, twin, con, guest])
    await db_session.flush()
    appearance = Appearance(
        id=uuid.uuid4(), convention_id=con.id, external_identity_id=guest.id
    )
    db_session.add(appearance)
    await db_session.commit()
    return World(user, paul, twin, guest, appearance)


@pytest.fixture
def service(db_session: AsyncSession) -> DecisionService:
    return DecisionService(db_session)


async def _decisions(session: AsyncSession) -> list[tuple[uuid.UUID, str]]:
    rows = await session.execute(
        select(IdentityDecision.person_id, IdentityDecision.decision)
    )
    return [(p, d) for p, d in rows]


async def _linked(session: AsyncSession, world: World) -> tuple[object, object]:
    await session.refresh(world.guest)
    await session.refresh(world.appearance)
    return world.guest.person_id, world.appearance.person_id


# --- the table --------------------------------------------------------------------


async def test_the_table_allows_one_confirmed_person_per_guest(
    db_session: AsyncSession, world: World
) -> None:
    for person, decision in ((world.paul, "confirmed"), (world.twin, "rejected")):
        db_session.add(
            IdentityDecision(
                external_identity_id=world.guest.id,
                person_id=person.id,
                decision=decision,
            )
        )
    await db_session.commit()

    db_session.add(
        IdentityDecision(
            external_identity_id=world.guest.id,
            person_id=uuid.uuid4(),
            decision="confirmed",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_a_guest_has_one_decision_per_person(
    db_session: AsyncSession, world: World
) -> None:
    for decision in ("rejected", "confirmed"):
        db_session.add(
            IdentityDecision(
                external_identity_id=world.guest.id,
                person_id=world.paul.id,
                decision=decision,
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.parametrize("decision", ["", "maybe", "Confirmed"])
async def test_an_unknown_decision_is_refused(
    db_session: AsyncSession, world: World, decision: str
) -> None:
    db_session.add(
        IdentityDecision(
            external_identity_id=world.guest.id,
            person_id=world.paul.id,
            decision=decision,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_deleting_a_person_or_a_guest_removes_their_decisions(
    db_session: AsyncSession, world: World, service: DecisionService
) -> None:
    await service.reject(world.guest.id, world.paul.id, world.user.id)
    await service.reject(world.guest.id, world.twin.id, world.user.id)

    await db_session.delete(world.paul)
    await db_session.commit()
    assert [d for _, d in await _decisions(db_session)] == ["rejected"]

    await db_session.delete(world.guest)
    await db_session.commit()
    assert await _decisions(db_session) == []


async def test_deleting_the_user_keeps_the_decision(
    db_session: AsyncSession, world: World, service: DecisionService
) -> None:
    await service.reject(world.guest.id, world.paul.id, world.user.id)

    await db_session.delete(world.user)
    await db_session.commit()

    stored = await db_session.scalar(select(IdentityDecision))
    assert stored is not None
    assert stored.decided_by is None


# --- confirming -------------------------------------------------------------------


async def test_confirming_links_the_guest_and_every_appearance(
    db_session: AsyncSession, world: World, service: DecisionService
) -> None:
    assert await service.confirm(world.guest.id, world.paul.id, world.user.id)

    assert await _linked(db_session, world) == (world.paul.id, world.paul.id)
    assert await _decisions(db_session) == [(world.paul.id, "confirmed")]
    stored = await db_session.scalar(select(IdentityDecision))
    assert stored is not None
    assert (stored.decided_by, stored.decided_at.tzinfo is not None) == (
        world.user.id,
        True,
    )


async def test_confirming_someone_else_replaces_the_link(
    db_session: AsyncSession, world: World, service: DecisionService
) -> None:
    await service.confirm(world.guest.id, world.paul.id, world.user.id)

    await service.confirm(world.guest.id, world.twin.id, world.user.id)

    assert await _linked(db_session, world) == (world.twin.id, world.twin.id)
    assert await _decisions(db_session) == [(world.twin.id, "confirmed")]


async def test_confirming_overturns_an_earlier_rejection(
    db_session: AsyncSession, world: World, service: DecisionService
) -> None:
    await service.reject(world.guest.id, world.paul.id, world.user.id)

    await service.confirm(world.guest.id, world.paul.id, world.user.id)

    assert await _decisions(db_session) == [(world.paul.id, "confirmed")]


async def test_confirming_twice_changes_nothing(
    db_session: AsyncSession, world: World, service: DecisionService
) -> None:
    await service.confirm(world.guest.id, world.paul.id, world.user.id)
    await service.confirm(world.guest.id, world.paul.id, world.user.id)

    assert await _decisions(db_session) == [(world.paul.id, "confirmed")]


# --- rejecting and unlinking ------------------------------------------------------


async def test_rejecting_records_it_and_leaves_the_guest_unlinked(
    db_session: AsyncSession, world: World, service: DecisionService
) -> None:
    assert await service.reject(world.guest.id, world.paul.id, world.user.id)

    assert await _decisions(db_session) == [(world.paul.id, "rejected")]
    assert await _linked(db_session, world) == (None, None)


async def test_rejecting_the_linked_person_removes_the_link(
    db_session: AsyncSession, world: World, service: DecisionService
) -> None:
    await service.confirm(world.guest.id, world.paul.id, world.user.id)

    await service.reject(world.guest.id, world.paul.id, world.user.id)

    assert await _decisions(db_session) == [(world.paul.id, "rejected")]
    assert await _linked(db_session, world) == (None, None)


async def test_rejecting_someone_else_keeps_the_link(
    db_session: AsyncSession, world: World, service: DecisionService
) -> None:
    await service.confirm(world.guest.id, world.paul.id, world.user.id)

    await service.reject(world.guest.id, world.twin.id, world.user.id)

    assert await _linked(db_session, world) == (world.paul.id, world.paul.id)


async def test_unlinking_removes_the_link_but_rejects_nobody(
    db_session: AsyncSession, world: World, service: DecisionService
) -> None:
    await service.confirm(world.guest.id, world.paul.id, world.user.id)
    await service.reject(world.guest.id, world.twin.id, world.user.id)

    assert await service.unlink(world.guest.id)

    assert await _linked(db_session, world) == (None, None)
    assert await _decisions(db_session) == [(world.twin.id, "rejected")]


async def test_a_rejection_can_be_taken_back_once(
    db_session: AsyncSession, world: World, service: DecisionService
) -> None:
    await service.reject(world.guest.id, world.paul.id, world.user.id)

    assert await service.forget_rejection(world.guest.id, world.paul.id)
    assert not await service.forget_rejection(world.guest.id, world.paul.id)
    assert await _decisions(db_session) == []


async def test_taking_back_does_not_remove_a_confirmation(
    db_session: AsyncSession, world: World, service: DecisionService
) -> None:
    await service.confirm(world.guest.id, world.paul.id, world.user.id)

    assert not await service.forget_rejection(world.guest.id, world.paul.id)
    assert await _decisions(db_session) == [(world.paul.id, "confirmed")]


async def test_unknown_guests_and_people_change_nothing(
    db_session: AsyncSession, world: World, service: DecisionService
) -> None:
    nobody = uuid.uuid4()

    assert not await service.confirm(nobody, world.paul.id, world.user.id)
    assert not await service.confirm(world.guest.id, nobody, world.user.id)
    assert not await service.reject(nobody, world.paul.id, world.user.id)
    assert not await service.reject(world.guest.id, nobody, world.user.id)
    assert not await service.unlink(nobody)

    assert await _decisions(db_session) == []
    assert await _linked(db_session, world) == (None, None)


# --- reading it back --------------------------------------------------------------


async def test_state_reports_the_link_and_the_rejections(
    world: World, service: DecisionService
) -> None:
    assert await service.state(world.guest.id) == (None, frozenset())

    await service.confirm(world.guest.id, world.paul.id, world.user.id)
    await service.reject(world.guest.id, world.twin.id, world.user.id)

    assert await service.state(world.guest.id) == (
        world.paul.id,
        frozenset({world.twin.id}),
    )


async def test_the_resolver_follows_the_stored_decisions(
    world: World, service: DecisionService
) -> None:
    people = [
        Candidate(world.paul.id, "Paul McGann"),
        Candidate(world.twin.id, "Paul McGann"),
    ]

    async def resolved() -> tuple[str, uuid.UUID | None]:
        linked, rejected = await service.state(world.guest.id)
        guest = Guest(
            "Paul McGann", linked_person_id=linked, rejected_person_ids=rejected
        )
        result = resolve(guest, people)
        return result.kind, result.person_id

    assert await resolved() == ("unresolved", None)
    await service.reject(world.guest.id, world.twin.id, world.user.id)
    assert await resolved() == ("possible", world.paul.id)
    await service.confirm(world.guest.id, world.paul.id, world.user.id)
    assert await resolved() == ("manual", world.paul.id)


# --- a sync never undoes a decision -----------------------------------------------


async def test_ingesting_again_never_changes_a_decision(
    db_session: AsyncSession, world: World, service: DecisionService
) -> None:
    await service.confirm(world.guest.id, world.paul.id, world.user.id)
    await service.reject(world.guest.id, world.twin.id, world.user.id)
    info = ConventionInfo(
        source_key="c",
        name="C",
        source_url="https://www.rostercon.com/en/event-convention/c",
    )
    listing = ListingResult(
        items=[
            GuestObservation(
                source_key="paul-mcgann-en",
                name="Paul McGann Renamed",
                source_url="https://www.rostercon.com/en/people/paul-mcgann-en",
            )
        ],
        complete=True,
    )

    for _ in range(3):
        await DiscoveryService(db_session).ingest("rostercon", info, listing)

    assert sorted(await _decisions(db_session)) == sorted(
        [(world.paul.id, "confirmed"), (world.twin.id, "rejected")]
    )
    assert (
        await db_session.scalar(select(func.count()).select_from(IdentityDecision)) == 2
    )
    guest = await db_session.get(ExternalIdentity, world.guest.id)
    assert guest is not None
    assert (guest.person_id, guest.display_name) == (
        world.paul.id,
        "Paul McGann Renamed",
    )
    appearances = list(
        await db_session.scalars(
            select(Appearance.person_id).where(
                Appearance.external_identity_id == world.guest.id
            )
        )
    )
    assert appearances
    assert set(appearances) == {world.paul.id}
