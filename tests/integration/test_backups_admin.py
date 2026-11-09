import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from starlette.testclient import TestClient

import casttrail.maintenance as maintenance
from casttrail.auth.service import AuthService
from casttrail.config import Settings
from casttrail.db import create_engine, create_session_factory
from casttrail.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


def _settings(tmp_path: Path, **changes: object) -> Settings:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'data' / 'app.db').as_posix()}"
    values: dict[str, object] = {
        "database_url": url,
        "admin_email": "admin@example.com",
        "admin_password": SecretStr(PASSWORD),
        **changes,
    }
    return Settings(**values)  # type: ignore[arg-type]


def _signed_in(
    settings: Settings, email: str = "admin@example.com"
) -> Iterator[TestClient]:
    with TestClient(
        create_app(settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        response = client.post("/login", data={"email": email, "password": PASSWORD})
        assert response.status_code == 303
        yield client


def _backup_names(tmp_path: Path) -> list[str]:
    folder = tmp_path / "data" / "backups"
    return sorted(p.name for p in folder.glob("*.db")) if folder.is_dir() else []


def test_the_page_lists_the_backups_taken_at_startup(tmp_path: Path) -> None:
    for client in _signed_in(_settings(tmp_path)):
        page = client.get("/admin/health").text

    assert "Backups" in page
    assert "Startup" in page
    assert "casttrail-" in page
    assert "Back up now" in page
    assert "stop the app" in page


def test_back_up_now_takes_a_manual_copy_and_says_so(tmp_path: Path) -> None:
    for client in _signed_in(_settings(tmp_path)):
        before = _backup_names(tmp_path)
        response = client.post("/admin/backups/now")
        page = client.get(response.headers["location"]).text

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/health?notice=backed-up"
    assert "A backup was taken." in page
    new = set(_backup_names(tmp_path)) - set(before)
    assert len(new) == 1
    assert next(iter(new)).endswith("-manual.db")
    assert "Manual" in page


def test_a_manual_copy_is_a_working_database(tmp_path: Path) -> None:
    for client in _signed_in(_settings(tmp_path)):
        client.post("/admin/backups/now")

    copy = next((tmp_path / "data" / "backups").glob("*-manual.db"))
    assert maintenance.migration_pending(copy) is False


def test_when_backups_are_off_the_page_says_so_and_the_button_does_nothing(
    tmp_path: Path,
) -> None:
    for client in _signed_in(_settings(tmp_path, backups_enabled=False)):
        page = client.get("/admin/health").text
        response = client.post("/admin/backups/now")
        followed = client.get(response.headers["location"]).text

    assert "Backups are switched off" in page
    assert "Back up now" not in page
    assert response.headers["location"] == "/admin/health?notice=backup-off"
    assert "Backups are switched off." in followed
    assert _backup_names(tmp_path) == []


def test_a_failed_manual_backup_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for client in _signed_in(_settings(tmp_path)):

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(maintenance, "backup", boom)
        response = client.post("/admin/backups/now")
        page = client.get(response.headers["location"]).text

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/health?notice=backup-failed"
    assert "The backup failed" in page
    assert "disk full" not in page


def test_an_unknown_notice_shows_nothing(tmp_path: Path) -> None:
    for client in _signed_in(_settings(tmp_path)):
        page = client.get("/admin/health?notice=%3Cb%3E").text

    assert "<b>" not in page
    assert 'role="status"' not in page


def test_startup_warnings_are_shown_to_the_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(maintenance, "backup", boom)
    for client in _signed_in(_settings(tmp_path)):
        page = client.get("/admin/health").text

    assert "Needs a look" in page
    assert "A backup could not be taken" in page
    assert "read-only" not in page


def test_no_warning_section_when_all_is_well(tmp_path: Path) -> None:
    for client in _signed_in(_settings(tmp_path)):
        page = client.get("/admin/health").text

    assert "Needs a look" not in page


def test_the_list_notes_each_kind_of_copy(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    folder = tmp_path / "data" / "backups"
    folder.mkdir(parents=True)
    when = datetime.now(UTC) - timedelta(hours=2)
    for reason in ("daily", "before-migration"):
        (folder / f"casttrail-{when:%Y%m%d-%H%M%S}-{reason}.db").write_bytes(
            b"x" * 2048
        )

    for client in _signed_in(settings):
        page = client.get("/admin/health").text

    assert "Before migration" in page
    assert "Daily" in page
    assert "2 KB" in page


async def _add_user(database_url: str) -> None:
    engine = create_engine(database_url)
    async with create_session_factory(engine)() as session:
        auth = AuthService(session, session_ttl=timedelta(hours=1))
        await auth.create_user(
            email="user@example.com", display_name="User", password=PASSWORD
        )
    await engine.dispose()


def test_signed_out_visitors_are_sent_to_sign_in(tmp_path: Path) -> None:
    with TestClient(
        create_app(_settings(tmp_path)), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        page = client.get("/admin/health")
        post = client.post("/admin/backups/now")

    assert (page.status_code, post.status_code) == (303, 303)
    assert not any(n.endswith("-manual.db") for n in _backup_names(tmp_path))


def test_an_ordinary_user_gets_a_404_and_no_copy_is_taken(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    for _ in _signed_in(settings):
        pass
    asyncio.run(_add_user(settings.database_url))

    for client in _signed_in(settings, "user@example.com"):
        page = client.get("/admin/health")
        post = client.post("/admin/backups/now")

    assert (page.status_code, post.status_code) == (404, 404)
    assert not any(n.endswith("-manual.db") for n in _backup_names(tmp_path))


def test_a_cross_site_post_is_refused_and_takes_no_copy(tmp_path: Path) -> None:
    for client in _signed_in(_settings(tmp_path)):
        response = client.post(
            "/admin/backups/now", headers={"Sec-Fetch-Site": "cross-site"}
        )

    assert response.status_code == 403
    assert not any(n.endswith("-manual.db") for n in _backup_names(tmp_path))
