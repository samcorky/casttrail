import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from starlette.testclient import TestClient

from casttrail.auth.service import AuthService
from casttrail.catalogue.models import Person
from casttrail.db import create_engine, create_session_factory
from casttrail.discovery import browse
from casttrail.discovery.browse import Guest, GuestSource, SourceView, _conflicts
from casttrail.discovery.models import Appearance, AppearanceSource, ConventionSource
from casttrail.discovery.service import DiscoveryService
from casttrail.identity.models import ExternalIdentity
from casttrail.main import create_app
from casttrail.matching.models import Match
from casttrail.matching.queries import relevant_appearances
from casttrail.providers import comiconomicon, rostercon

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.config import Settings

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"
FIXTURES = Path(__file__).parents[1] / "fixtures"
NOW = datetime(2026, 11, 9, tzinfo=UTC)


def _read(*parts: str) -> str:
    return (FIXTURES.joinpath(*parts)).read_text(encoding="utf-8")


async def _ingest_both(session: AsyncSession) -> uuid.UUID:
    roster = rostercon.parse_event_page(
        _read("rostercon", "event_follow-the-yellow-gate-road-take-4.html")
    )
    comicon = comiconomicon.parse_event_page(
        _read("comiconomicon", "event_42676_follow_the_yellow_gate_road.html")
    )
    assert roster.convention is not None
    assert comicon.convention is not None
    service = DiscoveryService(session)
    first = await service.ingest("rostercon", roster.convention, roster.guests)
    await service.ingest("comiconomicon", comicon.convention, comicon.guests)
    return first.convention_id


# --- what each source says --------------------------------------------------------


async def test_each_source_keeps_its_own_view_apart_from_the_merged_convention(
    db_session: AsyncSession,
) -> None:
    convention_id = await _ingest_both(db_session)

    sources = {
        s.provider: s
        for s in await db_session.scalars(
            select(ConventionSource).where(
                ConventionSource.convention_id == convention_id
            )
        )
    }

    assert set(sources) == {"rostercon", "comiconomicon"}
    assert sources["rostercon"].seen_name == "Follow The Yellow Gate Road (Take 4)"
    assert (sources["rostercon"].seen_start, sources["rostercon"].seen_end) == (
        date(2027, 4, 17),
        date(2027, 4, 18),
    )
    assert sources["comiconomicon"].seen_venue is not None
    assert "Lincoln" in sources["comiconomicon"].seen_venue
    assert sources["rostercon"].seen_venue != sources["comiconomicon"].seen_venue


async def test_a_view_is_updated_when_the_source_changes_its_page(
    db_session: AsyncSession,
) -> None:
    await _ingest_both(db_session)
    comicon = comiconomicon.parse_event_page(
        _read("comiconomicon", "event_42676_follow_the_yellow_gate_road.html")
    )
    assert comicon.convention is not None
    changed = comicon.convention.model_copy(update={"venue": "A New Hall"})

    await DiscoveryService(db_session).ingest("comiconomicon", changed, comicon.guests)

    source = await db_session.scalar(
        select(ConventionSource).where(ConventionSource.provider == "comiconomicon")
    )
    assert source is not None
    assert source.seen_venue == "A New Hall"


# --- conflicts --------------------------------------------------------------------


def _view(
    provider: str = "rostercon",
    name: str | None = "Some Con",
    start: date | None = date(2027, 4, 17),
    end: date | None = date(2027, 4, 18),
) -> SourceView:
    return SourceView(provider, provider, "u", name, start, end, None)


def test_sources_that_agree_have_no_conflict() -> None:
    views = [_view(), _view("comiconomicon", name="SOME  CON!")]

    assert _conflicts(views) == frozenset()


def test_a_fact_only_one_source_states_is_not_a_conflict() -> None:
    views = [_view(), _view("comiconomicon", name=None, start=None, end=None)]

    assert _conflicts(views) == frozenset()


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"name": "Other Con"}, {"name"}),
        ({"start": date(2027, 4, 18)}, {"start"}),
        ({"end": date(2027, 4, 19)}, {"end"}),
        (
            {"name": "Other", "start": date(2027, 5, 1), "end": date(2027, 5, 2)},
            {"name", "start", "end"},
        ),
    ],
)
def test_each_fact_the_sources_state_differently_is_named(
    changes: dict[str, object], expected: set[str]
) -> None:
    views = [_view(), _view("comiconomicon", **changes)]  # type: ignore[arg-type]

    assert _conflicts(views) == expected


# --- one guest across sources -----------------------------------------------------


def _source(
    provider: str, *, listed: bool = True, status: str | None = None
) -> GuestSource:
    return GuestSource(provider, provider, "u", listed, status)


@pytest.mark.parametrize(
    ("sources", "verdict", "disagree"),
    [
        ([_source("a")], "listed", False),
        ([_source("a", status="scheduled")], "confirmed", False),
        ([_source("a"), _source("b", status="scheduled")], "confirmed", False),
        ([_source("a", status="postponed")], "postponed", False),
        (
            [_source("a", status="scheduled"), _source("b", status="postponed")],
            "postponed",
            False,
        ),
        ([_source("a", listed=False)], "unlisted", False),
        ([_source("a", listed=False), _source("b")], "listed", False),
        ([_source("a", status="cancelled")], "cancelled", False),
        ([_source("a", status="cancelled"), _source("b")], "cancelled", True),
        (
            [_source("a", status="cancelled"), _source("b", listed=False)],
            "cancelled",
            False,
        ),
        (
            [_source("a", status="cancelled"), _source("b", status="cancelled")],
            "cancelled",
            False,
        ),
    ],
)
def test_a_guests_overall_status_is_only_what_the_sources_said(
    sources: list[GuestSource], verdict: str, disagree: bool
) -> None:
    guest = Guest("G", sources)

    assert (guest.verdict, guest.disagree) == (verdict, disagree)


async def test_the_same_guest_in_two_sources_is_shown_once(
    db_session: AsyncSession,
) -> None:
    convention_id = await _ingest_both(db_session)

    detail = await browse.event_detail(db_session, convention_id)

    assert detail is not None
    by_name = {g.name: [s.provider for s in g.sources] for g in detail.guests}
    assert by_name == {
        "Alexis Cruz": ["comiconomicon", "rostercon"],
        "Andrew Jackson": ["comiconomicon", "rostercon"],
        "David Read": ["comiconomicon"],
        "Michael Shanks": ["rostercon"],
    }
    assert [s.provider for s in detail.sources] == ["comiconomicon", "rostercon"]
    assert detail.conflicts == frozenset()


async def test_a_guest_linked_to_a_person_joins_a_listing_with_another_spelling(
    db_session: AsyncSession,
) -> None:
    convention_id = await _ingest_both(db_session)
    person = Person(id=uuid.uuid4(), name="Michael Shanks")
    db_session.add(person)
    await db_session.flush()
    for key in ("michael-shanks-en", "507"):
        identity = await db_session.scalar(
            select(ExternalIdentity).where(ExternalIdentity.external_id == key)
        )
        assert identity is not None
        identity.person_id = person.id
        for appearance in await db_session.scalars(
            select(Appearance).where(Appearance.external_identity_id == identity.id)
        ):
            appearance.person_id = person.id
    await db_session.commit()
    alexis = await db_session.scalar(
        select(ExternalIdentity).where(ExternalIdentity.external_id == "507")
    )
    assert alexis is not None
    alexis.display_name = "Alexis Cruz-Shanks"
    await db_session.commit()

    detail = await browse.event_detail(db_session, convention_id)

    assert detail is not None
    joined = [
        g
        for g in detail.guests
        if len(g.sources) == 2 and g.name in ("Michael Shanks", "Alexis Cruz-Shanks")
    ]
    assert len(joined) == 1


async def test_a_cancelled_guest_in_one_source_is_flagged_where_the_other_lists_them(
    db_session: AsyncSession,
) -> None:
    convention_id = await _ingest_both(db_session)
    line = await db_session.scalar(
        select(AppearanceSource)
        .join(Appearance, Appearance.id == AppearanceSource.appearance_id)
        .join(ExternalIdentity, ExternalIdentity.id == Appearance.external_identity_id)
        .where(
            AppearanceSource.provider == "rostercon",
            ExternalIdentity.display_name == "Andrew Jackson",
        )
    )
    assert line is not None
    line.status = "cancelled"
    await db_session.commit()

    detail = await browse.event_detail(db_session, convention_id)

    assert detail is not None
    andrew = next(g for g in detail.guests if g.name == "Andrew Jackson")
    assert (andrew.verdict, andrew.disagree) == ("cancelled", True)
    alexis = next(g for g in detail.guests if g.name == "Alexis Cruz")
    assert (alexis.verdict, alexis.disagree) == ("listed", False)


async def test_the_event_list_counts_a_shared_guest_once(
    db_session: AsyncSession,
) -> None:
    await _ingest_both(db_session)

    rows, _ = await browse.upcoming_events(
        db_session, today=date(2027, 4, 1), uk_only=False
    )

    assert [r.guests for r in rows] == [4]


# --- my appearances ---------------------------------------------------------------


async def test_a_person_at_a_merged_event_is_shown_once_with_the_surest_match(
    db_session: AsyncSession,
) -> None:
    convention_id = await _ingest_both(db_session)
    auth = AuthService(db_session, session_ttl=timedelta(hours=1))
    user = await auth.create_user(
        email="u@example.com", display_name="U", password=PASSWORD
    )
    user_id = user.id
    person = Person(id=uuid.uuid4(), name="Andrew Jackson")
    db_session.add(person)
    await db_session.flush()
    appearances = list(
        await db_session.scalars(
            select(Appearance)
            .join(
                ExternalIdentity, ExternalIdentity.id == Appearance.external_identity_id
            )
            .where(
                Appearance.convention_id == convention_id,
                ExternalIdentity.display_name == "Andrew Jackson",
            )
        )
    )
    assert len(appearances) == 2
    for kind, appearance in zip(("possible", "confirmed"), appearances, strict=True):
        db_session.add(
            Match(
                user_id=user_id,
                appearance_id=appearance.id,
                person_id=person.id,
                match_key=f"person:{person.id}",
                kind=kind,
                reason=kind,
            )
        )
    await db_session.commit()

    rows = await relevant_appearances(db_session, user_id, today=date(2027, 4, 1))

    assert [(r.person_name, r.kind) for r in rows] == [("Andrew Jackson", "confirmed")]


# --- on the page ------------------------------------------------------------------


def _seed(database_url: str) -> uuid.UUID:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")

    async def run() -> uuid.UUID:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            auth = AuthService(session, session_ttl=timedelta(hours=1))
            await auth.create_user(
                email="a@example.com", display_name="A", password=PASSWORD
            )
            convention_id = await _ingest_both(session)
            # Make the two sources disagree about the end day, as a merge by a person
            # of two differing listings could.
            source = await session.scalar(
                select(ConventionSource).where(
                    ConventionSource.provider == "comiconomicon"
                )
            )
            assert source is not None
            source.seen_end = date(2027, 4, 19)
            await session.commit()
        await engine.dispose()
        return convention_id

    return asyncio.run(run())


def test_the_page_shows_each_source_a_conflict_and_one_row_per_guest(
    settings: Settings, database_url: str
) -> None:
    convention_id = _seed(database_url)
    with TestClient(
        create_app(settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        client.post("/login", data={"email": "a@example.com", "password": PASSWORD})
        page = client.get(f"/events/{convention_id}").text

    assert "The sources disagree about the end" in " ".join(page.split())
    assert "Comiconomicon" in page
    assert "Rostercon" in page
    assert page.count("<td>Alexis Cruz</td>") == 1
    assert "<td>David Read</td>" in page
    assert "Sources disagree</strong>" not in page


def test_the_page_names_no_conflict_when_the_sources_agree(
    settings: Settings, database_url: str
) -> None:
    convention_id = _seed(database_url)

    async def undo() -> None:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            source = await session.scalar(
                select(ConventionSource).where(
                    ConventionSource.provider == "comiconomicon"
                )
            )
            assert source is not None
            source.seen_end = date(2027, 4, 18)
            await session.commit()
        await engine.dispose()

    asyncio.run(undo())
    with TestClient(
        create_app(settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        client.post("/login", data={"email": "a@example.com", "password": PASSWORD})
        page = client.get(f"/events/{convention_id}").text

    assert "The sources disagree" not in page


def test_an_old_source_row_without_a_view_shows_unknowns(
    settings: Settings, database_url: str
) -> None:
    convention_id = _seed(database_url)

    async def blank() -> None:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            for source in await session.scalars(select(ConventionSource)):
                source.seen_name = source.seen_start = source.seen_end = None
                source.seen_venue = None
            await session.commit()
        await engine.dispose()

    asyncio.run(blank())
    with TestClient(
        create_app(settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        client.post("/login", data={"email": "a@example.com", "password": PASSWORD})
        page = client.get(f"/events/{convention_id}").text

    assert page.count("<td>Unknown</td>") == 2
    assert page.count("Date unknown") >= 2
    assert page.count("Place unknown") >= 2
    assert "The sources disagree" not in page


async def _rename(session: AsyncSession, key: str, name: str) -> None:
    identity = await session.scalar(
        select(ExternalIdentity).where(ExternalIdentity.external_id == key)
    )
    assert identity is not None
    identity.display_name = name
    await session.commit()


async def test_the_same_name_in_another_case_is_still_one_guest(
    db_session: AsyncSession,
) -> None:
    convention_id = await _ingest_both(db_session)
    await _rename(db_session, "alexis-cruz-en", "ALEXIS  cruz")

    detail = await browse.event_detail(db_session, convention_id)
    rows, _ = await browse.upcoming_events(
        db_session, today=date(2027, 4, 1), uk_only=False
    )

    assert detail is not None
    assert [len(g.sources) for g in detail.guests if "cruz" in g.name.casefold()] == [2]
    assert [r.guests for r in rows] == [4]


async def test_an_unlinked_listing_joins_the_linked_one_of_the_same_name(
    db_session: AsyncSession,
) -> None:
    convention_id = await _ingest_both(db_session)
    person = Person(id=uuid.uuid4(), name="Andrew Jackson")
    db_session.add(person)
    await db_session.flush()
    identity = await db_session.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.external_id == "andrew-jackson-en"
        )
    )
    assert identity is not None
    identity.person_id = person.id
    for appearance in await db_session.scalars(
        select(Appearance).where(Appearance.external_identity_id == identity.id)
    ):
        appearance.person_id = person.id
    await db_session.commit()

    detail = await browse.event_detail(db_session, convention_id)
    rows, _ = await browse.upcoming_events(
        db_session, today=date(2027, 4, 1), uk_only=False
    )

    assert detail is not None
    jackson = [g for g in detail.guests if g.name == "Andrew Jackson"]
    assert [sorted(s.provider for s in g.sources) for g in jackson] == [
        ["comiconomicon", "rostercon"]
    ]
    assert [r.guests for r in rows] == [4]
