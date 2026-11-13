import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2
import pytest
import structlog
from sqlalchemy import func, select

from casttrail.catalogue.models import Person
from casttrail.discovery.lookup import FRESH_FOR, GuestLookup
from casttrail.discovery.models import SourceLookup
from casttrail.providers.base import ProviderError
from casttrail.providers.comiconomicon_client import (
    ComiconomiconFetcher,
    ComiconomiconUnavailableError,
)
from casttrail.providers.polite import create_client

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession
    from time_machine import TimeMachineFixture

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[1] / "fixtures" / "comiconomicon"
START = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
GUEST = (FIXTURES / "autocomplete_guest_andrew_jackson.json").read_text()
NOBODY = "[]"


def _two_guests() -> str:
    """The same name twice, as two different guests."""
    entries = [
        {
            "id": str(n),
            "name": "Andrew Jackson",
            "type": "guest",
            "data1": None,
            "data2": None,
            "data3": None,
            "appearances": 1,
            "new": 0,
            "url": f"/guest/{n}/Andrew_Jackson",
        }
        for n in (518, 999)
    ]
    return json.dumps(entries)


class FakeComiconomicon:
    """Answers every search with the current body, and remembers what was asked."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.body = GUEST
        self.status = 200
        self.requests: list[httpx2.Request] = []
        # Whether the session still had a transaction open when a request was made.
        self.open_during_request: list[bool] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        self.open_during_request.append(self.session.in_transaction())
        return httpx2.Response(self.status, text=self.body)

    def lookup(self) -> GuestLookup:
        client = create_client(transport=httpx2.MockTransport(self.handler))
        return GuestLookup(self.session, ComiconomiconFetcher(client, min_interval=0.0))


@pytest.fixture(autouse=True)
def _start_the_clock(time_machine: TimeMachineFixture) -> None:
    time_machine.move_to(START, tick=False)


@pytest.fixture
def site(db_session: AsyncSession) -> FakeComiconomicon:
    return FakeComiconomicon(db_session)


async def _person(session: AsyncSession, name: str = "Andrew Jackson") -> uuid.UUID:
    person = Person(name=name)
    session.add(person)
    await session.commit()
    return person.id


async def _stored(session: AsyncSession) -> list[SourceLookup]:
    return list(await session.scalars(select(SourceLookup)))


# --- finding and remembering ------------------------------------------------------


async def test_a_guest_is_found_and_the_answer_is_kept(
    db_session: AsyncSession, site: FakeComiconomicon
) -> None:
    person_id = await _person(db_session)

    entry = await site.lookup().find(person_id, "Andrew Jackson")

    assert (entry.outcome, entry.source_key) == ("found", "518")
    assert entry.source_url == "https://comiconomicon.com/guest/518/Andrew_Jackson"
    stored = await _stored(db_session)
    assert [(s.provider, s.person_id, s.outcome) for s in stored] == [
        ("comiconomicon", person_id, "found")
    ]
    assert stored[0].looked_up_at == START


async def test_a_name_nobody_has_is_remembered_as_not_found(
    db_session: AsyncSession, site: FakeComiconomicon
) -> None:
    site.body = NOBODY
    person_id = await _person(db_session)

    entry = await site.lookup().find(person_id, "Andrew Jackson")

    assert (entry.outcome, entry.source_key, entry.source_url) == (
        "not_found",
        None,
        None,
    )


async def test_two_guests_with_the_name_are_ambiguous_and_neither_is_picked(
    db_session: AsyncSession, site: FakeComiconomicon
) -> None:
    site.body = _two_guests()
    person_id = await _person(db_session)

    entry = await site.lookup().find(person_id, "Andrew Jackson")

    assert (entry.outcome, entry.source_key, entry.source_url) == (
        "ambiguous",
        None,
        None,
    )


async def test_a_fresh_answer_is_used_without_asking_the_site_again(
    db_session: AsyncSession, site: FakeComiconomicon
) -> None:
    person_id = await _person(db_session)
    lookup = site.lookup()
    first = await lookup.find(person_id, "Andrew Jackson")

    again = await lookup.find(person_id, "Andrew Jackson")

    assert again.id == first.id
    assert len(site.requests) == 1


# --- how long an answer is trusted ------------------------------------------------


@pytest.mark.parametrize(
    ("body", "outcome", "days"),
    [(GUEST, "found", 180), (NOBODY, "not_found", 7), (_two_guests(), "ambiguous", 30)],
)
async def test_an_answer_is_trusted_for_its_time_and_then_asked_again(
    db_session: AsyncSession,
    site: FakeComiconomicon,
    time_machine: TimeMachineFixture,
    body: str,
    outcome: str,
    days: int,
) -> None:
    site.body = body
    person_id = await _person(db_session)
    lookup = site.lookup()
    await lookup.find(person_id, "Andrew Jackson")
    assert FRESH_FOR[outcome] == timedelta(days=days)

    time_machine.move_to(START + timedelta(days=days) - timedelta(minutes=1))
    await lookup.find(person_id, "Andrew Jackson")
    assert len(site.requests) == 1

    time_machine.move_to(START + timedelta(days=days) + timedelta(minutes=1))
    await lookup.find(person_id, "Andrew Jackson")
    assert len(site.requests) == 2


async def test_a_not_found_becomes_found_once_the_site_knows_the_guest(
    db_session: AsyncSession,
    site: FakeComiconomicon,
    time_machine: TimeMachineFixture,
) -> None:
    site.body = NOBODY
    person_id = await _person(db_session)
    lookup = site.lookup()
    await lookup.find(person_id, "Andrew Jackson")

    site.body = GUEST
    time_machine.move_to(START + timedelta(days=8))
    entry = await lookup.find(person_id, "Andrew Jackson")

    assert (entry.outcome, entry.source_key) == ("found", "518")
    stored = await _stored(db_session)
    assert len(stored) == 1
    assert stored[0].looked_up_at == START + timedelta(days=8)


# --- when the site cannot answer ----------------------------------------------------


async def test_a_refusal_stores_nothing_and_is_raised(
    db_session: AsyncSession, site: FakeComiconomicon
) -> None:
    site.status = 503
    person_id = await _person(db_session)

    with pytest.raises(ComiconomiconUnavailableError):
        await site.lookup().find(person_id, "Andrew Jackson")

    assert await _stored(db_session) == []


async def test_an_unreadable_answer_is_an_error_and_not_taken_for_not_found(
    db_session: AsyncSession, site: FakeComiconomicon
) -> None:
    site.body = json.dumps({"error": "changed"})
    person_id = await _person(db_session)

    with pytest.raises(ProviderError, match="did not answer with a list"):
        await site.lookup().find(person_id, "Andrew Jackson")

    assert await _stored(db_session) == []


async def test_an_old_answer_survives_when_asking_again_fails(
    db_session: AsyncSession,
    site: FakeComiconomicon,
    time_machine: TimeMachineFixture,
) -> None:
    person_id = await _person(db_session)
    lookup = site.lookup()
    await lookup.find(person_id, "Andrew Jackson")

    site.status = 503
    time_machine.move_to(START + timedelta(days=200))
    with pytest.raises(ComiconomiconUnavailableError):
        await lookup.find(person_id, "Andrew Jackson")

    kept = await _stored(db_session)
    assert [(k.outcome, k.source_key, k.looked_up_at) for k in kept] == [
        ("found", "518", START)
    ]


# --- people stay separate ----------------------------------------------------------


async def test_each_person_has_their_own_answer(
    db_session: AsyncSession, site: FakeComiconomicon
) -> None:
    andrew = await _person(db_session)
    nobody = await _person(db_session, "Someone Unknown")
    lookup = site.lookup()

    await lookup.find(andrew, "Andrew Jackson")
    await lookup.find(nobody, "Someone Unknown")

    assert await db_session.scalar(select(func.count()).select_from(SourceLookup)) == 2
    outcomes = {s.person_id: s.outcome for s in await _stored(db_session)}
    assert outcomes == {andrew: "found", nobody: "not_found"}


# --- the database is free while the site is slow -----------------------------------


async def test_no_transaction_is_open_while_the_site_is_asked(
    db_session: AsyncSession,
    site: FakeComiconomicon,
    time_machine: TimeMachineFixture,
) -> None:
    person_id = await _person(db_session)
    lookup = site.lookup()

    await lookup.find(person_id, "Andrew Jackson")
    time_machine.move_to(START + timedelta(days=200))
    await lookup.find(person_id, "Andrew Jackson")

    assert site.open_during_request == [False, False]


# --- logging -----------------------------------------------------------------------


async def test_a_lookup_logs_its_outcome_and_whether_it_came_from_the_cache(
    db_session: AsyncSession, site: FakeComiconomicon
) -> None:
    person_id = await _person(db_session)
    lookup = site.lookup()

    with structlog.testing.capture_logs() as events:
        await lookup.find(person_id, "Andrew Jackson")
        await lookup.find(person_id, "Andrew Jackson")

    lookups = [e for e in events if e["event"] == "guest_lookup"]
    assert [(e["outcome"], e["cached"]) for e in lookups] == [
        ("found", False),
        ("found", True),
    ]
    assert all(e["person_id"] == str(person_id) for e in lookups)
    assert "Andrew" not in json.dumps(events, default=str)
