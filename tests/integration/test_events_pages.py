import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from starlette.testclient import TestClient

from casttrail.auth.service import AuthService
from casttrail.db import create_engine, create_session_factory
from casttrail.discovery import browse
from casttrail.discovery.models import (
    Appearance,
    AppearanceSource,
    Convention,
    ConventionSource,
)
from casttrail.identity.models import ExternalIdentity
from casttrail.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.config import Settings

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"
TODAY = date(2026, 11, 8)
NOW = datetime(2026, 11, 8, tzinfo=UTC)
UK = "united-kingdom"


def _con(name: str, start: date | None, **fields: object) -> Convention:
    fields.setdefault("country", UK)
    return Convention(
        id=uuid.uuid4(),
        name=name,
        start_date=start,
        created_at=NOW,
        updated_at=NOW,
        **fields,
    )


def _guest(
    con: Convention,
    key: str,
    name: str | None,
    *,
    listed: bool = True,
    status: str | None = None,
) -> list[object]:
    identity = ExternalIdentity(
        id=uuid.uuid4(), provider="rostercon", external_id=key, display_name=name
    )
    appearance = Appearance(
        id=uuid.uuid4(), convention_id=con.id, external_identity_id=identity.id
    )
    line = AppearanceSource(
        id=uuid.uuid4(),
        appearance_id=appearance.id,
        provider="rostercon",
        source_url=f"https://www.rostercon.com/en/people/{key}",
        listed=listed,
        status=status,
    )
    return [identity, appearance, line]


def _seed(database_url: str, items: list[object]) -> None:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")

    async def run() -> None:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            auth = AuthService(session, session_ttl=timedelta(hours=1))
            await auth.create_user(
                email="alice@example.com", display_name="Alice", password=PASSWORD
            )
            for item in items:
                # Parents first, so foreign keys hold.
                session.add(item)
                await session.flush()
            await session.commit()
        await engine.dispose()

    asyncio.run(run())


def _client(
    settings: Settings, database_url: str, items: list[object]
) -> Iterator[TestClient]:
    _seed(database_url, items)
    with TestClient(
        create_app(settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as c:
        assert (
            c.post(
                "/login", data={"email": "alice@example.com", "password": PASSWORD}
            ).status_code
            == 303
        )
        yield c


# --- the list ---------------------------------------------------------------------


def test_the_pages_need_a_sign_in(settings: Settings, database_url: str) -> None:
    _seed(database_url, [])
    with TestClient(create_app(settings), follow_redirects=False) as client:
        for path in ("/events", f"/events/{uuid.uuid4()}"):
            response = client.get(path)
            assert (response.status_code, response.headers["location"]) == (
                303,
                "/login",
            )


def test_the_list_shows_what_is_known_and_unknowns_as_unknown(
    settings: Settings, database_url: str
) -> None:
    today = datetime.now(UTC).date()
    known = _con(
        "Known Con",
        today + timedelta(days=5),
        city="Lincoln",
        venue="A Hotel",
        status="cancelled",
    )
    bare = _con("Bare Con", None)
    items: list[object] = [known, bare]
    for client in _client(settings, database_url, items):
        page = client.get("/events").text

    assert "Known Con" in page
    assert "A Hotel, Lincoln" in page
    assert "<strong>Cancelled</strong>" in page
    assert page.index("Known Con") < page.index("Bare Con")
    assert "Date unknown" in page
    assert "Place unknown" in page
    assert "Status unknown" in page


def test_a_one_day_and_a_multi_day_event_are_formatted(
    settings: Settings, database_url: str
) -> None:
    today = datetime.now(UTC).date()
    a = _con("One Day", today + timedelta(days=3))
    b = _con("Two Days", today + timedelta(days=4), end_date=today + timedelta(days=5))
    for client in _client(settings, database_url, [a, b]):
        page = client.get("/events").text

    assert (today + timedelta(days=3)).strftime("%d %b %Y") in page
    assert " to " in page


def test_only_uk_events_are_listed_by_default_and_all_on_request(
    settings: Settings, database_url: str
) -> None:
    today = datetime.now(UTC).date()
    items: list[object] = [
        _con("Britain Con", today + timedelta(days=3)),
        _con("France Con", today + timedelta(days=3), country="france"),
        _con("Nowhere Con", today + timedelta(days=3), country=None),
    ]
    for client in _client(settings, database_url, items):
        uk = client.get("/events").text
        everything = client.get("/events?scope=all").text

    assert "Britain Con" in uk
    assert "France Con" not in uk
    assert "Nowhere Con" not in uk
    assert all(
        name in everything for name in ("Britain Con", "France Con", "Nowhere Con")
    )
    assert "Show all countries" in uk
    assert "Show UK only" in everything


def test_finished_events_are_hidden_but_ones_just_started_or_running_are_not(
    settings: Settings, database_url: str
) -> None:
    today = datetime.now(UTC).date()
    items: list[object] = [
        _con("Long Gone", today - timedelta(days=30)),
        _con(
            "Just Ended", today - timedelta(days=3), end_date=today - timedelta(days=3)
        ),
        _con("Started Yesterday", today - timedelta(days=1)),
        _con(
            "Still Running",
            today - timedelta(days=5),
            end_date=today + timedelta(days=1),
        ),
    ]
    for client in _client(settings, database_url, items):
        page = client.get("/events").text

    assert "Long Gone" not in page
    assert "Just Ended" not in page
    assert "Started Yesterday" in page
    assert "Still Running" in page


def test_the_guest_count_counts_only_listed_guests(
    settings: Settings, database_url: str
) -> None:
    con = _con("Counted Con", datetime.now(UTC).date() + timedelta(days=3))
    items: list[object] = [
        con,
        *_guest(con, "a-en", "A"),
        *_guest(con, "b-en", "B"),
        *_guest(con, "c-en", "C", listed=False),
    ]
    for client in _client(settings, database_url, items):
        page = client.get("/events").text

    row = page[page.index("Counted Con") :]
    assert '<td class="text-end">2</td>' in row[: row.index("</tr>")]


def test_an_empty_list_says_so(settings: Settings, database_url: str) -> None:
    for client in _client(settings, database_url, []):
        page = client.get("/events").text

    assert "No events found yet" in page
    assert "/admin/refresh" not in page


def test_paging(
    settings: Settings, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browse, "PAGE_SIZE", 2)
    today = datetime.now(UTC).date()
    items: list[object] = [
        _con(f"Con {n}", today + timedelta(days=n)) for n in range(1, 6)
    ]
    for client in _client(settings, database_url, items):
        first = client.get("/events").text
        second = client.get("/events?page=2").text
        last = client.get("/events?page=3").text
        all_second = client.get("/events?scope=all&page=2").text

    assert "Con 1" in first
    assert "Con 3" not in first
    assert "page=2" in first
    assert "Earlier" not in first
    assert "Con 3" in second
    assert "Earlier" in second
    assert "Later" in second
    assert "Con 5" in last
    assert "Later" not in last
    assert "scope=all&amp;page=3" in all_second
    assert "scope=all&amp;page=1" in all_second


@pytest.mark.parametrize("query", ["page=0", "page=1001", "page=x", "scope=mars"])
def test_bad_query_values_are_refused(
    settings: Settings, database_url: str, query: str
) -> None:
    for client in _client(settings, database_url, []):
        assert client.get(f"/events?{query}").status_code == 422


# --- one event --------------------------------------------------------------------


def test_an_event_page_shows_its_details_source_and_guests(
    settings: Settings, database_url: str
) -> None:
    con = _con(
        "Detail Con",
        date(2027, 4, 17),
        end_date=date(2027, 4, 18),
        venue="A Hotel",
        city="Lincoln",
        region="England",
        organizer="Mama Nox",
        status="scheduled",
    )
    source = ConventionSource(
        convention_id=con.id,
        provider="rostercon",
        source_key="detail-con",
        source_url="https://www.rostercon.com/en/event-convention/detail-con",
    )
    items: list[object] = [
        con,
        source,
        *_guest(con, "zed-en", "Zed Guest", status="scheduled"),
        *_guest(con, "amy-en", "Amy Guest"),
        *_guest(con, "bob-en", "Bob Guest", status="cancelled"),
        *_guest(con, "cat-en", "Cat Guest", status="postponed"),
        *_guest(con, "dan-en", "Dan Guest", listed=False),
        *_guest(con, "nameless-en", None),
    ]
    for client in _client(settings, database_url, items):
        page = client.get(f"/events/{con.id}").text

    assert "17 Apr 2027 to 18 Apr 2027" in page
    assert "A Hotel, Lincoln (England)" in page
    assert "Mama Nox" in page
    assert "Scheduled" in page
    assert "https://www.rostercon.com/en/event-convention/detail-con" in page
    order = [
        "Amy Guest",
        "Bob Guest",
        "Cat Guest",
        "nameless-en",
        "Zed Guest",
        "Dan Guest",
    ]
    assert "<td>nameless-en</td>" in page
    order[3] = "<td>nameless-en</td>"
    assert sorted(order, key=page.index) == order
    # Listed guests come first, then ones no longer listed.
    assert page.index("Zed Guest") < page.index("Dan Guest")
    assert page.index("nameless-en") < page.index("Dan Guest")
    assert "Confirmed" in page
    assert "<strong>Cancelled</strong>" in page
    assert "Postponed" in page
    assert "No longer listed" in page


def test_a_guest_who_dropped_off_is_not_called_cancelled(
    settings: Settings, database_url: str
) -> None:
    con = _con("Drop Con", date(2027, 1, 1))
    items: list[object] = [con, *_guest(con, "dan-en", "Dan Guest", listed=False)]
    for client in _client(settings, database_url, items):
        page = client.get(f"/events/{con.id}").text

    body = page[page.index("Dan Guest") :]
    assert "No longer listed" in body
    assert "Cancelled" not in body[: body.index("</tr>")]


def test_a_bare_event_page_shows_unknowns_and_no_guests(
    settings: Settings, database_url: str
) -> None:
    con = _con("Bare Con", None)
    for client in _client(settings, database_url, [con]):
        page = client.get(f"/events/{con.id}").text

    for text in (
        "Date unknown",
        "Place unknown",
        "Status unknown",
        "Unknown",
        "No guests are known",
    ):
        assert text in page
    assert "No source is recorded for this event." in page


def test_an_unknown_event_is_a_404(settings: Settings, database_url: str) -> None:
    for client in _client(settings, database_url, []):
        assert client.get(f"/events/{uuid.uuid4()}").status_code == 404
        assert client.get("/events/not-a-uuid").status_code == 422


def test_an_event_does_not_show_another_events_guests(
    settings: Settings, database_url: str
) -> None:
    one = _con("One Con", date(2027, 1, 1))
    two = _con("Two Con", date(2027, 1, 2))
    items: list[object] = [one, two, *_guest(one, "a-en", "Only At One")]
    for client in _client(settings, database_url, items):
        page = client.get(f"/events/{two.id}").text

    assert "Only At One" not in page


def test_names_from_a_source_are_escaped(settings: Settings, database_url: str) -> None:
    con = _con("<b>Bold</b> Con", date(2027, 1, 1))
    items: list[object] = [con, *_guest(con, "x-en", "<script>alert(1)</script>")]
    for client in _client(settings, database_url, items):
        page = client.get(f"/events/{con.id}").text

    assert "<script>alert" not in page
    assert "<b>Bold</b>" not in page


def test_the_nav_has_an_events_link(settings: Settings, database_url: str) -> None:
    for client in _client(settings, database_url, []):
        page = client.get("/events").text

    assert 'href="/events" aria-current="page"' in page


async def test_upcoming_events_counts_from_the_given_day(
    db_session: AsyncSession,
) -> None:
    db_session.add(_con("Edge Con", TODAY - timedelta(days=2)))
    db_session.add(_con("Past Con", TODAY - timedelta(days=3)))
    await db_session.commit()

    rows, more = await browse.upcoming_events(db_session, today=TODAY)

    assert [r.convention.name for r in rows] == ["Edge Con"]
    assert more is False
