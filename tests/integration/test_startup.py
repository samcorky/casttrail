import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import structlog
import structlog.testing
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from starlette.testclient import TestClient

import casttrail.maintenance as maintenance
from casttrail import startup
from casttrail.auth.models import User
from casttrail.config import Settings
from casttrail.db import create_engine, create_session_factory
from casttrail.main import _start_scheduler, create_app
from casttrail.startup import (
    BACKUP_FIRST_PROBLEM,
    BACKUP_WARNING,
    CHECK_PROBLEM,
    FOLDER_PROBLEM,
    MIGRATE_PROBLEM,
    StartupReport,
    maintenance_page,
    prepare_database,
    run_backup,
    run_backup_quietly,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"
PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


def _settings(tmp_path: Path, **changes: object) -> Settings:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'data' / 'app.db').as_posix()}"
    return Settings(database_url=url, **changes)  # type: ignore[arg-type]


def _backups(tmp_path: Path) -> list[str]:
    folder = tmp_path / "data" / "backups"
    return (
        sorted(p.name.split("-", 3)[3] for p in folder.glob("*.db"))
        if folder.is_dir()
        else []
    )


def _old_database(tmp_path: Path, revision: str = "0004") -> Path:
    """A database as an older version of the app left it, with a user in it."""
    file = tmp_path / "data" / "app.db"
    file.parent.mkdir(parents=True)
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{file.as_posix()}")
    command.upgrade(config, revision)
    with closing(sqlite3.connect(file)) as db:
        db.execute(
            "INSERT INTO users VALUES (?, ?, ?, ?, 1, 1, ?)",
            ("1" * 32, "a@example.com", "A", "h", "2026-11-08 12:00:00"),
        )
        db.commit()
    return file


# --- preparing the database -------------------------------------------------------


async def test_a_new_database_is_created_migrated_and_backed_up(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    report = await prepare_database(settings)

    assert report == StartupReport()
    file = tmp_path / "data" / "app.db"
    assert maintenance.migration_pending(file) is False
    assert _backups(tmp_path) == ["startup.db"]


async def test_an_old_database_is_copied_before_it_is_updated(tmp_path: Path) -> None:
    file = await asyncio.to_thread(_old_database, tmp_path)
    settings = _settings(tmp_path)

    report = await prepare_database(settings)

    assert report.problems == []
    assert maintenance.migration_pending(file) is False
    assert _backups(tmp_path) == ["before-migration.db"]
    (copy,) = (tmp_path / "data" / "backups").glob("*.db")
    assert maintenance.current_revision(copy) == "0004"
    with closing(sqlite3.connect(copy)) as db:
        assert db.execute("SELECT email FROM users").fetchall() == [("a@example.com",)]


async def test_an_up_to_date_database_with_a_recent_backup_is_left_alone(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    await prepare_database(settings)
    before = _backups(tmp_path)

    report = await prepare_database(settings)

    assert report == StartupReport()
    assert _backups(tmp_path) == before


async def test_an_old_enough_backup_is_replaced_at_startup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await prepare_database(settings)
    folder = tmp_path / "data" / "backups"
    for item in folder.glob("*.db"):
        item.rename(folder / "casttrail-20200101-000000-daily.db")

    await prepare_database(settings)

    assert _backups(tmp_path) == ["daily.db", "startup.db"]


async def test_switching_auto_migrate_off_leaves_the_database_alone(
    tmp_path: Path,
) -> None:
    file = await asyncio.to_thread(_old_database, tmp_path)

    report = await prepare_database(_settings(tmp_path, auto_migrate=False))

    assert report.problems == []
    assert maintenance.current_revision(file) == "0004"
    assert _backups(tmp_path) == ["startup.db"]


async def test_switching_backups_off_makes_no_backup_but_still_migrates(
    tmp_path: Path,
) -> None:
    file = await asyncio.to_thread(_old_database, tmp_path)

    report = await prepare_database(_settings(tmp_path, backups_enabled=False))

    assert report.problems == []
    assert maintenance.migration_pending(file) is False
    assert not (tmp_path / "data" / "backups").exists()


async def test_backups_can_go_to_a_folder_of_your_choosing(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"

    await prepare_database(_settings(tmp_path, backup_dir=str(elsewhere)))

    assert len(list(elsewhere.glob("*.db"))) == 1
    assert not (tmp_path / "data" / "backups").exists()


async def test_an_in_memory_database_needs_nothing(tmp_path: Path) -> None:
    report = await prepare_database(
        Settings(database_url="sqlite+aiosqlite:///:memory:")
    )

    assert report == StartupReport()


# --- things going wrong never raise -----------------------------------------------


async def test_an_uncreatable_data_folder_is_a_problem_not_a_crash(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "data"
    blocker.write_text("a file where the folder should be", encoding="utf-8")

    report = await prepare_database(_settings(tmp_path))

    assert report.problems == [FOLDER_PROBLEM]


async def test_a_failed_update_is_a_problem_that_names_no_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = await asyncio.to_thread(_old_database, tmp_path)

    def boom(url: str) -> None:
        raise RuntimeError(f"secret detail about {url}")

    monkeypatch.setattr(maintenance, "migrate", boom)
    with structlog.testing.capture_logs() as logs:
        report = await prepare_database(_settings(tmp_path))

    assert report.problems == [MIGRATE_PROBLEM]
    assert "secret detail" not in " ".join(report.problems)
    assert "migration_failed" in [entry["event"] for entry in logs]
    assert maintenance.current_revision(file) == "0004"
    assert _backups(tmp_path) == ["before-migration.db"]


async def test_no_update_is_attempted_when_the_safety_copy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = await asyncio.to_thread(_old_database, tmp_path)
    migrated: list[str] = []

    def no_backup(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(maintenance, "backup", no_backup)
    monkeypatch.setattr(maintenance, "migrate", migrated.append)

    report = await prepare_database(_settings(tmp_path))

    assert report.problems == [BACKUP_FIRST_PROBLEM]
    assert migrated == []
    assert maintenance.current_revision(file) == "0004"


async def test_a_database_from_a_newer_version_is_a_problem_and_is_not_touched(
    tmp_path: Path,
) -> None:
    file = tmp_path / "data" / "app.db"
    file.parent.mkdir(parents=True)
    with closing(sqlite3.connect(file)) as db:
        db.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        db.execute("INSERT INTO alembic_version VALUES ('9999')")
        db.commit()

    report = await prepare_database(_settings(tmp_path))

    assert report.problems == [MIGRATE_PROBLEM]
    assert maintenance.current_revision(file) == "9999"


async def test_a_check_that_fails_is_a_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(file: Path) -> bool:
        raise RuntimeError("no")

    monkeypatch.setattr(maintenance, "migration_pending", boom)

    report = await prepare_database(_settings(tmp_path))

    assert report.problems == [CHECK_PROBLEM]


async def test_a_failed_startup_backup_is_only_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(maintenance, "backup", boom)

    report = await prepare_database(_settings(tmp_path))

    assert report.problems == []
    assert report.warnings == [BACKUP_WARNING]
    assert maintenance.migration_pending(tmp_path / "data" / "app.db") is False


# --- backups on demand and on a timer ---------------------------------------------


async def test_run_backup_returns_nothing_when_switched_off(tmp_path: Path) -> None:
    assert (
        await run_backup(_settings(tmp_path, backups_enabled=False), "manual") is None
    )
    assert (
        await run_backup(
            Settings(database_url="sqlite+aiosqlite:///:memory:"), "manual"
        )
        is None
    )


async def test_run_backup_prunes_old_daily_copies(tmp_path: Path) -> None:
    settings = _settings(tmp_path, backup_keep=2)
    await prepare_database(_settings(tmp_path, backups_enabled=False))
    folder = tmp_path / "data" / "backups"
    folder.mkdir()
    for day in range(1, 5):
        (folder / f"casttrail-2026010{day}-000000-daily.db").write_bytes(b"x")

    await run_backup(settings, "daily")

    daily = [b for b in maintenance.list_backups(folder) if b.reason == "daily"]
    assert len(daily) == 2
    assert daily[0].taken_at.year >= 2026


async def test_the_quiet_backup_logs_a_failure_and_does_not_raise(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    with structlog.testing.capture_logs() as logs:
        await run_backup_quietly(settings, "daily")

    assert "backup_failed" in [entry["event"] for entry in logs]


async def test_the_quiet_backup_logs_success(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await prepare_database(settings)

    with structlog.testing.capture_logs() as logs:
        await run_backup_quietly(settings, "daily")

    taken = [entry for entry in logs if entry["event"] == "backup_taken"]
    assert [entry["reason"] for entry in taken] == ["daily"]


async def test_the_daily_backup_is_scheduled_at_the_chosen_hour(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, backup_hour=5)

    scheduler = _start_scheduler(settings, object())  # type: ignore[arg-type]

    assert scheduler is not None
    try:
        (job,) = scheduler.get_jobs()
        assert (job.id, job.func) == ("daily-backup", run_backup_quietly)
        assert job.args == (settings, "daily")
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert (fields["hour"], fields["minute"]) == ("5", "30")
        assert (job.max_instances, job.coalesce, job.misfire_grace_time) == (
            1,
            True,
            3600,
        )
    finally:
        scheduler.shutdown(wait=False)


async def test_no_timers_at_all_when_both_jobs_are_off(tmp_path: Path) -> None:
    settings = _settings(tmp_path, backups_enabled=False, refresh_enabled=False)

    assert _start_scheduler(settings, object()) is None  # type: ignore[arg-type]


async def test_both_timers_run_side_by_side(tmp_path: Path) -> None:
    settings = _settings(tmp_path, refresh_enabled=True)

    scheduler = _start_scheduler(settings, object())  # type: ignore[arg-type]

    assert scheduler is not None
    try:
        assert sorted(j.id for j in scheduler.get_jobs()) == [
            "daily-backup",
            "refresh",
        ]
    finally:
        scheduler.shutdown(wait=False)


# --- the app itself ---------------------------------------------------------------


def _app(settings: Settings) -> Iterator[TestClient]:
    with TestClient(
        create_app(settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        yield client


def test_the_app_starts_on_an_empty_folder_and_serves(tmp_path: Path) -> None:
    for client in _app(_settings(tmp_path)):
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/login").status_code == 200


def test_a_startup_problem_shows_a_plain_page_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(url: str) -> None:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(maintenance, "migrate", boom)

    for client in _app(_settings(tmp_path)):
        page = client.get("/login")
        home = client.get("/")
        health = client.get("/health")
        asset = client.get("/static/css/app.css")

    assert page.status_code == 503
    assert home.status_code == 503
    assert "needs attention" in page.text
    assert "could not be updated" in page.text
    assert "secret detail" not in page.text
    assert page.headers["cache-control"] == "no-store"
    assert (health.status_code, health.json()) == (503, {"status": "degraded"})
    assert asset.status_code == 200


def test_a_problem_also_blocks_posts_and_unknown_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(maintenance, "migrate", lambda url: 1 / 0)

    for client in _app(_settings(tmp_path)):
        assert (
            client.post("/login", data={"email": "a", "password": "b"}).status_code
            == 503
        )
        assert client.get("/no/such/page").status_code == 503


def test_the_problem_page_escapes_what_it_shows() -> None:
    page = maintenance_page(["<script>alert(1)</script> & more"])

    assert b"<script>" not in page.body
    assert b"&lt;script&gt;" in page.body
    assert page.status_code == 503


def test_no_problem_means_pages_are_served_normally(tmp_path: Path) -> None:
    for client in _app(_settings(tmp_path)):
        assert client.get("/login").status_code == 200


def test_invalid_settings_give_a_page_naming_the_setting_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("CASTTRAIL_REFRESH_HOUR", "99")
    monkeypatch.setenv("CASTTRAIL_ADMIN_PASSWORD", "super-secret-value")
    monkeypatch.delenv("CASTTRAIL_ADMIN_EMAIL", raising=False)
    monkeypatch.chdir(tmp_path)

    with TestClient(create_app()) as client:
        page = client.get("/")
        health = client.get("/health")

    assert page.status_code == 503
    assert "CASTTRAIL_REFRESH_HOUR" in page.text
    assert "99" not in page.text
    assert "super-secret-value" not in page.text
    assert (health.status_code, health.json()) == (503, {"status": "degraded"})


def test_a_scheduler_that_cannot_start_is_a_warning_and_the_app_still_serves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import casttrail.main as main

    def boom(settings: object, refresher: object) -> None:
        raise RuntimeError("no timers")

    monkeypatch.setattr(main, "_start_scheduler", boom)
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert app.state.startup.warnings == [startup.TIMERS_WARNING]


def test_no_timers_are_started_while_there_is_a_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(maintenance, "migrate", lambda url: 1 / 0)
    app = create_app(_settings(tmp_path))

    with TestClient(app):
        assert app.state.scheduler is None


def test_a_first_admin_is_created_after_an_automatic_update(tmp_path: Path) -> None:
    from pydantic import SecretStr

    settings = _settings(
        tmp_path,
        admin_email="admin@example.com",
        admin_password=SecretStr(PASSWORD),
        backups_enabled=False,
    )

    for client in _app(settings):
        response = client.post(
            "/login", data={"email": "admin@example.com", "password": PASSWORD}
        )

    assert response.status_code == 303


def test_an_existing_database_keeps_its_data_through_an_automatic_update(
    tmp_path: Path,
) -> None:
    _old_database(tmp_path)

    async def users() -> list[str]:
        engine = create_engine(_settings(tmp_path).database_url)
        async with create_session_factory(engine)() as session:
            found = list(await session.scalars(select(User.email)))
        await engine.dispose()
        return found

    for client in _app(_settings(tmp_path)):
        assert client.get("/health").status_code == 200

    assert asyncio.run(users()) == ["a@example.com"]


async def test_the_quiet_backup_says_nothing_when_backups_are_off(
    tmp_path: Path,
) -> None:
    with structlog.testing.capture_logs() as logs:
        await run_backup_quietly(_settings(tmp_path, backups_enabled=False), "daily")

    assert logs == []
    assert not (tmp_path / "data").exists()


async def test_a_failed_update_does_not_also_take_a_startup_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await asyncio.to_thread(_old_database, tmp_path)
    folder = tmp_path / "data" / "backups"
    folder.mkdir()
    (folder / "casttrail-20200101-000000-daily.db").write_bytes(b"x")
    monkeypatch.setattr(maintenance, "migrate", lambda url: 1 / 0)

    report = await prepare_database(_settings(tmp_path))

    assert report.problems == [MIGRATE_PROBLEM]
    assert _backups(tmp_path) == ["before-migration.db", "daily.db"]


async def test_a_failed_safety_copy_is_only_a_problem_not_also_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await asyncio.to_thread(_old_database, tmp_path)

    def no_backup(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(maintenance, "backup", no_backup)

    report = await prepare_database(_settings(tmp_path))

    assert report.problems == [BACKUP_FIRST_PROBLEM]
    assert report.warnings == []


async def test_an_empty_database_with_auto_migrate_off_has_nothing_to_back_up(
    tmp_path: Path,
) -> None:
    report = await prepare_database(_settings(tmp_path, auto_migrate=False))

    assert report == StartupReport()
    assert _backups(tmp_path) == []
