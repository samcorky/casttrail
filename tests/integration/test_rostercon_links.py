import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import structlog
from sqlalchemy import func, select

from casttrail.auth.models import User
from casttrail.catalogue.models import Person
from casttrail.discovery.links import (
    InvalidLinkError,
    RosterconLinks,
    UnknownPersonError,
)
from casttrail.discovery.models import SourceLookup
from casttrail.providers.rostercon import PersonPage
from casttrail.watching.models import Watch

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from time_machine import TimeMachineFixture

pytestmark = pytest.mark.integration

START = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
TOM_URL = "https://www.rostercon.com/en/people/tom-wilson-en"
TOM = PersonPage(slug="tom-wilson-en", url=TOM_URL)


@pytest.fixture(autouse=True)
def _start_the_clock(time_machine: TimeMachineFixture) -> None:
    time_machine.move_to(START, tick=False)


async def _user(session: AsyncSession) -> uuid.UUID:
    user = User(
        email=f"{uuid.uuid4()}@example.com",
        display_name="U",
        password_hash="x",
        is_active=True,
        is_admin=False,
    )
    session.add(user)
    await session.flush()
    return user.id


async def _person(
    session: AsyncSession, name: str = "Thomas F. Wilson", *, watched: bool = True
) -> uuid.UUID:
    person = Person(name=name)
    session.add(person)
    await session.flush()
    if watched:
        session.add(Watch(user_id=await _user(session), person_id=person.id))
    await session.commit()
    return person.id


async def _rows(session: AsyncSession) -> list[SourceLookup]:
    return list(await session.scalars(select(SourceLookup)))


# --- setting a link ----------------------------------------------------------------


async def test_a_pasted_address_is_stored_as_a_manual_link(
    db_session: AsyncSession,
) -> None:
    person_id = await _person(db_session)

    page = await RosterconLinks(db_session).link(person_id, f"  {TOM_URL}?x=1  ")

    assert page == TOM
    [row] = await _rows(db_session)
    assert (row.provider, row.person_id, row.outcome, row.manual) == (
        "rostercon",
        person_id,
        "found",
        True,
    )
    assert (row.source_key, row.source_url, row.looked_up_at) == (
        "tom-wilson-en",
        TOM_URL,
        START,
    )
    assert await RosterconLinks(db_session).manual(person_id) == TOM


@pytest.mark.parametrize(
    "text",
    ["", "tom-wilson", "http://www.rostercon.com/en/people/tom", "https://evil.test/x"],
)
async def test_something_that_is_not_a_person_page_is_refused_and_stores_nothing(
    db_session: AsyncSession, text: str
) -> None:
    person_id = await _person(db_session)

    with pytest.raises(InvalidLinkError):
        await RosterconLinks(db_session).link(person_id, text)

    assert await _rows(db_session) == []


async def test_linking_someone_who_does_not_exist_is_refused(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(UnknownPersonError):
        await RosterconLinks(db_session).link(uuid.uuid4(), TOM_URL)

    assert await _rows(db_session) == []


async def test_linking_again_replaces_the_page_and_keeps_one_row(
    db_session: AsyncSession,
) -> None:
    person_id = await _person(db_session)
    links = RosterconLinks(db_session)
    await links.link(person_id, TOM_URL)

    await links.link(person_id, "https://www.rostercon.com/en/people/tom-wilson-2-en")

    [row] = await _rows(db_session)
    assert row.source_key == "tom-wilson-2-en"


async def test_a_manual_link_replaces_what_a_refresh_found_by_name(
    db_session: AsyncSession,
) -> None:
    person_id = await _person(db_session)
    links = RosterconLinks(db_session)
    await links.remember(person_id, "not_found")

    await links.link(person_id, TOM_URL)

    [row] = await _rows(db_session)
    assert (row.outcome, row.manual, row.source_key) == ("found", True, "tom-wilson-en")


# --- removing a link ---------------------------------------------------------------


async def test_unlinking_removes_a_manual_link(db_session: AsyncSession) -> None:
    person_id = await _person(db_session)
    links = RosterconLinks(db_session)
    await links.link(person_id, TOM_URL)

    assert await links.unlink(person_id) is True

    assert await _rows(db_session) == []
    assert await links.manual(person_id) is None


async def test_unlinking_never_removes_what_a_refresh_found(
    db_session: AsyncSession,
) -> None:
    person_id = await _person(db_session)
    links = RosterconLinks(db_session)
    await links.remember(person_id, "found", TOM)

    assert await links.unlink(person_id) is False

    assert len(await _rows(db_session)) == 1


async def test_unlinking_one_person_leaves_another_alone(
    db_session: AsyncSession,
) -> None:
    tom = await _person(db_session)
    other = await _person(db_session, "Someone Else")
    links = RosterconLinks(db_session)
    await links.link(tom, TOM_URL)
    await links.link(other, "https://www.rostercon.com/en/people/someone-else-en")

    await links.unlink(tom)

    assert await links.manual(tom) is None
    assert await links.manual(other) is not None


# --- what a refresh learns ---------------------------------------------------------


@pytest.mark.parametrize("outcome", ["not_found", "ambiguous"])
async def test_a_refresh_remembers_a_person_it_could_not_place(
    db_session: AsyncSession, outcome: str
) -> None:
    person_id = await _person(db_session)

    await RosterconLinks(db_session).remember(person_id, outcome)

    [row] = await _rows(db_session)
    assert (row.outcome, row.manual, row.source_key, row.source_url) == (
        outcome,
        False,
        None,
        None,
    )


async def test_a_refresh_remembers_the_page_it_found_and_updates_it(
    db_session: AsyncSession, time_machine: TimeMachineFixture
) -> None:
    person_id = await _person(db_session)
    links = RosterconLinks(db_session)
    await links.remember(person_id, "not_found")

    time_machine.move_to(START + timedelta(days=1))
    await links.remember(person_id, "found", TOM)

    [row] = await _rows(db_session)
    assert (row.outcome, row.source_key, row.looked_up_at) == (
        "found",
        "tom-wilson-en",
        START + timedelta(days=1),
    )
    assert row.manual is False


@pytest.mark.parametrize("outcome", ["not_found", "ambiguous", "found"])
async def test_a_refresh_never_replaces_a_link_set_by_hand(
    db_session: AsyncSession, outcome: str
) -> None:
    person_id = await _person(db_session)
    links = RosterconLinks(db_session)
    await links.link(person_id, TOM_URL)
    other = PersonPage(slug="other", url="https://www.rostercon.com/en/people/other")

    await links.remember(person_id, outcome, other if outcome == "found" else None)

    [row] = await _rows(db_session)
    assert (row.manual, row.source_key, row.looked_up_at) == (
        True,
        "tom-wilson-en",
        START,
    )


# --- listing ------------------------------------------------------------------------


async def test_the_list_shows_each_watched_person_once_with_their_state(
    db_session: AsyncSession,
) -> None:
    tom = await _person(db_session, "Thomas F. Wilson")
    kirsten = await _person(db_session, "Kirsten Cooke")
    paul = await _person(db_session, "Paul McGann")
    amy = await _person(db_session, "Amy Unlooked")
    await _person(db_session, "Not Watched", watched=False)
    links = RosterconLinks(db_session)
    await links.link(tom, TOM_URL)
    await links.remember(kirsten, "not_found")
    paul_page = PersonPage(
        slug="paul-mcgann-en", url="https://www.rostercon.com/en/people/paul-mcgann-en"
    )
    await links.remember(paul, "found", paul_page)

    rows = await links.watched()

    assert [(r.name, r.state, r.slug) for r in rows] == [
        ("Amy Unlooked", "unknown", None),
        ("Kirsten Cooke", "not_found", None),
        ("Paul McGann", "found", "paul-mcgann-en"),
        ("Thomas F. Wilson", "manual", "tom-wilson-en"),
    ]
    assert {r.person_id for r in rows} == {tom, kirsten, paul, amy}


async def test_a_person_watched_by_two_users_is_listed_once(
    db_session: AsyncSession,
) -> None:
    person_id = await _person(db_session)
    db_session.add(Watch(user_id=await _user(db_session), person_id=person_id))
    await db_session.commit()

    rows = await RosterconLinks(db_session).watched()

    assert [r.person_id for r in rows] == [person_id]


async def test_only_one_row_is_kept_per_person(db_session: AsyncSession) -> None:
    person_id = await _person(db_session)
    links = RosterconLinks(db_session)

    await links.remember(person_id, "not_found")
    await links.remember(person_id, "ambiguous")
    await links.link(person_id, TOM_URL)

    assert await db_session.scalar(select(func.count()).select_from(SourceLookup)) == 1


# --- logging ------------------------------------------------------------------------


async def test_setting_and_removing_a_link_are_logged_without_the_address(
    db_session: AsyncSession,
) -> None:
    person_id = await _person(db_session)
    links = RosterconLinks(db_session)

    with structlog.testing.capture_logs() as events:
        await links.link(person_id, TOM_URL)
        await links.unlink(person_id)

    assert [e["event"] for e in events] == [
        "rostercon_link_set",
        "rostercon_link_removed",
    ]
    assert all(e["person_id"] == str(person_id) for e in events)
    assert "http" not in str(events)
