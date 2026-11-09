import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from starlette.testclient import TestClient

from casttrail.auth.service import AuthService
from casttrail.db import create_engine, create_session_factory
from casttrail.discovery.health import STALE_AFTER, SourceHealth, rostercon_health
from casttrail.discovery.models import (
    Convention,
    ConventionSource,
    JobRun,
    SourceObservation,
)
from casttrail.main import create_app

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
    return await rostercon_health(session, now=NOW)


# --- the summary ------------------------------------------------------------------


async def test_a_fresh_install_says_nothing_has_run(db_session: AsyncSession) -> None:
    health = await rostercon_health(db_session, now=NOW)

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

    health = await rostercon_health(db_session, now=NOW)

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

    health = await rostercon_health(db_session, now=NOW)

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

    health = await rostercon_health(db_session, now=NOW)

    assert health.conventions == 1
    assert health.last_page_at == NOW - timedelta(hours=1)


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

    health = await rostercon_health(db_session, now=NOW)

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

    health = await rostercon_health(db_session, now=NOW)

    assert health.warnings == ["No refresh has run yet."]


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
            session.add(
                JobRun(
                    job="rostercon-refresh",
                    status="succeeded",
                    started_at=datetime.now(UTC) - timedelta(hours=1),
                    summary=json.dumps({"events_unreadable": 1}),
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

    assert "Rostercon" in page
    assert "changed its layout" in page
    assert "Stored data was kept" in page
    assert "Failed refreshes in a row" in page
    assert 'href="/admin/health" aria-current="page"' in page
