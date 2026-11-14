import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
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
from casttrail.discovery.health import (
    COMICONOMICON,
    ROSTERCON,
    STALE_AFTER,
    SourceHealth,
    source_health,
)
from casttrail.discovery.models import (
    Convention,
    ConventionSource,
    JobRun,
    SourceLookup,
    SourceObservation,
)
from casttrail.main import create_app
from casttrail.providers.base import PersonDetail
from casttrail.watching.service import WatchService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.config import Settings

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"


def _run(
    status: str, ago: timedelta, counts: dict[str, object] | None = None
) -> JobRun:
    return JobRun(
        job="rostercon-refresh",
        status=status,
        started_at=NOW - ago,
        summary=json.dumps(counts) if counts is not None else None,
    )


async def _health(session: AsyncSession, *runs: JobRun) -> SourceHealth:
    session.add_all(runs)
    await session.commit()
    return await source_health(session, ROSTERCON, now=NOW)


# --- the summary ------------------------------------------------------------------


async def test_a_fresh_install_says_nothing_has_run(db_session: AsyncSession) -> None:
    health = await source_health(db_session, ROSTERCON, now=NOW)

    assert health.warnings == ["No refresh has run yet."]
    assert (health.conventions, health.last_run_at, health.last_success_at) == (
        0,
        None,
        None,
    )
    assert health.failures_in_a_row == 0


async def test_a_clean_recent_run_has_no_warnings(db_session: AsyncSession) -> None:
    db_session.add(_run("succeeded", timedelta(hours=2), {"stopped": None}))
    await db_session.commit()

    health = await source_health(db_session, ROSTERCON, now=NOW)

    assert health.warnings == []
    assert health.last_run_status == "succeeded"
    assert health.last_success_at == NOW - timedelta(hours=2)


async def test_a_run_still_in_progress_is_not_judged(db_session: AsyncSession) -> None:
    db_session.add_all(
        [
            _run("succeeded", timedelta(hours=30), {"stopped": None}),
            _run("running", timedelta(minutes=5)),
        ]
    )
    await db_session.commit()

    health = await source_health(db_session, ROSTERCON, now=NOW)

    assert health.last_run_status == "succeeded"
    assert health.warnings == []


async def test_it_counts_stored_conventions_and_the_last_page_read(
    db_session: AsyncSession,
) -> None:
    con = Convention(id=uuid.uuid4(), name="C", created_at=NOW, updated_at=NOW)
    other = Convention(id=uuid.uuid4(), name="D", created_at=NOW, updated_at=NOW)
    db_session.add_all([con, other])
    await db_session.flush()
    db_session.add_all(
        [
            ConventionSource(
                convention_id=con.id,
                provider="rostercon",
                source_key="c",
                source_url="https://www.rostercon.com/en/event-convention/c",
            ),
            ConventionSource(
                convention_id=other.id,
                provider="comiconomicon",
                source_key="d",
                source_url="https://comiconomicon.com/d",
            ),
            SourceObservation(
                provider="rostercon",
                source_key="c",
                content_hash="a" * 64,
                complete=True,
                fetched_at=NOW - timedelta(days=2),
            ),
            SourceObservation(
                provider="rostercon",
                source_key="c",
                content_hash="b" * 64,
                complete=True,
                fetched_at=NOW - timedelta(hours=1),
            ),
            SourceObservation(
                provider="comiconomicon",
                source_key="d",
                content_hash="c" * 64,
                complete=True,
                fetched_at=NOW,
            ),
        ]
    )
    await db_session.commit()

    health = await source_health(db_session, ROSTERCON, now=NOW)
    second = await source_health(db_session, COMICONOMICON, now=NOW)

    assert health.conventions == 1
    assert health.last_page_at == NOW - timedelta(hours=1)
    # Each source counts only what it supplied.
    assert (second.conventions, second.last_page_at) == (1, NOW)


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({"stopped": "Rostercon asked us to wait"}, "stopped early: Rostercon asked"),
        ({"events_unreadable": 2}, "changed its layout"),
        ({"events_failed": 1}, "could not be fetched"),
        ({"people_failed": 1}, "could not be fetched"),
        ({"people_ambiguous": 1}, "no single Rostercon page"),
        ({"people_unmatched": 3}, "no single Rostercon page"),
    ],
)
async def test_each_problem_in_the_last_run_gets_its_own_warning(
    db_session: AsyncSession, counts: dict[str, object], expected: str
) -> None:
    health = await _health(db_session, _run("succeeded", timedelta(hours=1), counts))

    assert any(expected in warning for warning in health.warnings)


async def test_the_layout_warning_says_stored_data_was_kept(
    db_session: AsyncSession,
) -> None:
    health = await _health(
        db_session, _run("succeeded", timedelta(hours=1), {"events_unreadable": 1})
    )

    assert any("Stored data was kept" in w for w in health.warnings)


@pytest.mark.parametrize(
    "counts",
    [{}, {"events_unreadable": 0}, {"events_unreadable": True}, {"stopped": 5}],
)
async def test_zero_or_malformed_counts_raise_no_warning(
    db_session: AsyncSession, counts: dict[str, object]
) -> None:
    health = await _health(db_session, _run("succeeded", timedelta(hours=1), counts))

    assert health.warnings == []


@pytest.mark.parametrize("stored", [None, "not json", "[1]", '"x"'])
async def test_a_missing_or_damaged_record_does_not_break_the_summary(
    db_session: AsyncSession, stored: str | None
) -> None:
    db_session.add(
        JobRun(
            job="rostercon-refresh",
            status="succeeded",
            started_at=NOW - timedelta(hours=1),
            summary=stored,
        )
    )
    await db_session.commit()

    health = await source_health(db_session, ROSTERCON, now=NOW)

    assert health.warnings == []


async def test_failed_runs_in_a_row_are_counted_and_a_success_ends_the_streak(
    db_session: AsyncSession,
) -> None:
    health = await _health(
        db_session,
        _run("failed", timedelta(hours=1), {"stopped": "x"}),
        _run("failed", timedelta(hours=25), {"stopped": "x"}),
        _run("succeeded", timedelta(hours=49), {}),
        _run("failed", timedelta(hours=73), {"stopped": "x"}),
    )

    assert health.failures_in_a_row == 2
    assert health.last_success_at == NOW - timedelta(hours=49)


async def test_no_success_among_recent_runs_is_a_warning(
    db_session: AsyncSession,
) -> None:
    health = await _health(db_session, _run("failed", timedelta(hours=1), {}))

    assert "No refresh has succeeded recently." in health.warnings
    assert health.last_success_at is None


async def test_an_old_success_is_a_warning_only_after_the_limit(
    db_session: AsyncSession,
) -> None:
    older = STALE_AFTER + timedelta(minutes=1)
    health = await _health(db_session, _run("succeeded", older, {}))

    assert any("more than 3 days" in w for w in health.warnings)


async def test_a_success_just_inside_the_limit_is_fine(
    db_session: AsyncSession,
) -> None:
    health = await _health(db_session, _run("succeeded", STALE_AFTER, {}))

    assert health.warnings == []


async def test_other_jobs_are_ignored(db_session: AsyncSession) -> None:
    db_session.add(
        JobRun(job="other", status="failed", started_at=NOW - timedelta(hours=1))
    )
    await db_session.commit()

    health = await source_health(db_session, ROSTERCON, now=NOW)

    assert health.warnings == ["No refresh has run yet."]


async def test_comiconomicon_is_judged_on_its_own_runs_and_named_in_its_warnings(
    db_session: AsyncSession,
) -> None:
    db_session.add_all(
        [
            _run("succeeded", timedelta(hours=1), {}),
            JobRun(
                job="comiconomicon-refresh",
                status="failed",
                started_at=NOW - timedelta(hours=2),
                summary=json.dumps({"events_unreadable": 1, "people_unmatched": 2}),
            ),
        ]
    )
    await db_session.commit()

    rostercon = await source_health(db_session, ROSTERCON, now=NOW)
    comiconomicon = await source_health(db_session, COMICONOMICON, now=NOW)

    assert rostercon.warnings == []
    assert (comiconomicon.label, comiconomicon.failures_in_a_row) == (
        "Comiconomicon",
        1,
    )
    assert comiconomicon.last_run_status == "failed"
    assert comiconomicon.warnings == [
        "Some event pages could not be read. "
        + "Comiconomicon may have changed its layout. Stored data was kept.",
        "Some watched people have no single Comiconomicon page; skipped.",
        "No refresh has succeeded recently.",
    ]


# --- timings and who was skipped (A9) ----------------------------------------------


async def test_the_last_runs_duration_and_steps_are_read_from_its_record(
    db_session: AsyncSession,
) -> None:
    run = _run(
        "succeeded",
        timedelta(hours=2),
        {
            "steps": {
                "event_pages": {
                    "count": 3,
                    "requests": 3,
                    "waited_s": 9.5,
                    "answering_s": 1,
                },
                "lookup": {"count": 1, "requests": 3, "waited_s": 0, "answering_s": 2},
            }
        },
    )
    run.finished_at = run.started_at + timedelta(minutes=4)

    health = await _health(db_session, run)

    assert health.last_run_duration_s == 240
    assert [(s.label, s.count, s.requests) for s in health.steps] == [
        ("Name lookup", 1, 3),
        ("Event pages", 3, 3),
    ]
    assert [(s.waited_s, s.answering_s) for s in health.steps] == [
        (0.0, 2.0),
        (9.5, 1.0),
    ]


@pytest.mark.parametrize(
    "steps",
    [
        None,
        "lots",
        {"lookup": "fast"},
        {"lookup": {"count": "one", "waited_s": "long", "answering_s": True}},
        {"somewhere_else": {"count": 1}},
    ],
)
async def test_damaged_step_timings_are_shown_as_nothing_or_zero(
    db_session: AsyncSession, steps: object
) -> None:
    health = await _health(
        db_session, _run("succeeded", timedelta(hours=2), {"steps": steps})
    )

    assert all(
        (s.count, s.requests, s.waited_s, s.answering_s) == (0, 0, 0.0, 0.0)
        for s in health.steps
    )
    assert [s.label for s in health.steps] in ([], ["Name lookup"])


async def test_a_run_that_never_finished_has_no_duration(
    db_session: AsyncSession,
) -> None:
    health = await _health(db_session, _run("failed", timedelta(hours=2)))

    assert (health.last_run_duration_s, health.steps) == (None, [])


async def _watched(session: AsyncSession, name: str, tmdb_id: int) -> uuid.UUID:
    user = await AuthService(session, session_ttl=timedelta(hours=1)).create_user(
        email=f"{tmdb_id}@example.com", display_name=name, password=PASSWORD
    )
    await WatchService(session).watch_person(
        user.id, PersonDetail(tmdb_id=tmdb_id, name=name)
    )
    person_id = await session.scalar(select(Person.id).where(Person.name == name))
    assert person_id is not None
    return person_id


def _lookup(provider: str, person_id: uuid.UUID, outcome: str) -> SourceLookup:
    return SourceLookup(
        provider=provider, person_id=person_id, outcome=outcome, looked_up_at=NOW
    )


async def test_the_skipped_warning_names_who_was_skipped_and_why(
    db_session: AsyncSession,
) -> None:
    wilson = await _watched(db_session, "Thomas F. Wilson", 1)
    cooke = await _watched(db_session, "Kirsten Cooke", 2)
    found = await _watched(db_session, "Paul McGann", 3)
    unwatched = Person(name="Nobody Watches")
    db_session.add(unwatched)
    await db_session.flush()
    db_session.add_all(
        [
            _lookup("rostercon", wilson, "not_found"),
            _lookup("rostercon", cooke, "ambiguous"),
            _lookup("rostercon", unwatched.id, "not_found"),
            # Comiconomicon's answers are its own.
            _lookup("comiconomicon", found, "not_found"),
            SourceLookup(
                provider="rostercon",
                person_id=found,
                outcome="found",
                source_key="paul-mcgann-en",
                source_url="https://www.rostercon.com/en/people/paul-mcgann-en",
                looked_up_at=NOW,
            ),
        ]
    )

    health = await _health(
        db_session, _run("succeeded", timedelta(hours=2), {"people_unmatched": 1})
    )

    names = "Kirsten Cooke (several found), Thomas F. Wilson (not found)"
    assert health.warnings == [
        "These watched people have no single Rostercon page and were skipped: "
        + f"{names}. A page can be set by hand on Source pages."
    ]


async def test_the_skipped_warning_still_shows_when_nobody_can_be_named(
    db_session: AsyncSession,
) -> None:
    health = await _health(
        db_session, _run("succeeded", timedelta(hours=2), {"people_unmatched": 1})
    )

    assert health.warnings == [
        "Some watched people have no single Rostercon page; skipped."
    ]


# --- the page ---------------------------------------------------------------------


def _seed(database_url: str) -> None:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")

    async def run() -> None:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            auth = AuthService(session, session_ttl=timedelta(hours=1))
            await auth.create_user(
                email="admin@example.com",
                display_name="Admin",
                password=PASSWORD,
                is_admin=True,
            )
            await auth.create_user(
                email="user@example.com", display_name="User", password=PASSWORD
            )
            started = datetime.now(UTC) - timedelta(hours=1)
            steps = {
                "event_pages": {
                    "count": 2,
                    "requests": 2,
                    "waited_s": 150.0,
                    "answering_s": 1.25,
                },
                "lookup": {
                    "count": 1,
                    "requests": 3,
                    "waited_s": 4000.0,
                    "answering_s": 12.4,
                },
            }
            session.add(
                JobRun(
                    job="rostercon-refresh",
                    status="succeeded",
                    started_at=started,
                    finished_at=started + timedelta(seconds=90),
                    summary=json.dumps({"events_unreadable": 1, "steps": steps}),
                )
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(run())


def test_the_page_is_for_administrators_only(
    settings: Settings, database_url: str
) -> None:
    _seed(database_url)
    with TestClient(
        create_app(settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        assert client.get("/admin/health").status_code == 303
        client.post("/login", data={"email": "user@example.com", "password": PASSWORD})
        assert client.get("/admin/health").status_code == 404


def test_an_admin_sees_the_summary_and_its_warnings(
    settings: Settings, database_url: str
) -> None:
    _seed(database_url)
    with TestClient(
        create_app(settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        client.post("/login", data={"email": "admin@example.com", "password": PASSWORD})
        page = client.get("/admin/health").text

    rostercon = page[page.index('<h2 class="h4">Rostercon</h2>') :]
    comiconomicon = rostercon[rostercon.index('<h2 class="h4">Comiconomicon</h2>') :]
    rostercon = rostercon[: len(rostercon) - len(comiconomicon)]
    assert "Rostercon may have changed its layout" in rostercon
    assert "No refresh has run yet." in comiconomicon
    assert "changed its layout" not in comiconomicon
    assert "Stored data was kept" in page
    assert "Failed refreshes in a row" in page
    # How long the last run took, and its steps in the order a person goes through.
    assert 'Last refresh took</dt><dd class="col-sm-8 col-lg-9">1 min 30 s' in rostercon
    assert "Last refresh took" not in comiconomicon
    assert rostercon.index("Name lookup") < rostercon.index("Event pages")
    for shown in ("1 h 6 min", "12 s", "2 min 30 s", "1.2 s"):
        assert f'<td class="text-end">{shown}</td>' in rostercon
    assert 'href="/admin/health" aria-current="page"' in page
