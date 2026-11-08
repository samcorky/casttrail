import asyncio
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from starlette.testclient import TestClient

from casttrail.auth.models import AuthSession, KnownDevice, User
from casttrail.auth.service import AuthService
from casttrail.db import create_engine, create_session_factory
from casttrail.main import create_app
from casttrail.web.dependencies import CurrentUser  # noqa: TC001 - FastAPI needs it

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.config import Settings

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
NEW_PASSWORD = "a brand new passphrase"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"


def _seed(database_url: str) -> None:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")

    async def run() -> None:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            auth = AuthService(session, session_ttl=timedelta(hours=1))
            for email in ("alice@example.com", "bob@example.com"):
                await auth.create_user(
                    email=email, display_name=email.split("@")[0], password=PASSWORD
                )
        await engine.dispose()

    asyncio.run(run())


def _count(database_url: str, what: str, email: str) -> int:
    """Count a user's sessions or remembered devices straight from the database."""

    async def run() -> int:
        engine = create_engine(database_url)
        async with create_session_factory(engine)() as session:
            model = AuthSession if what == "sessions" else KnownDevice
            count = await session.scalar(
                select(func.count(model.id))
                .join(User, User.id == model.user_id)
                .where(User.email == email)
            )
        await engine.dispose()
        return count or 0

    return asyncio.run(run())


def _app(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.get("/whoami")
    async def whoami(user: CurrentUser) -> dict[str, str]:
        return {"email": user.email}

    return app


@pytest.fixture
def client(settings: Settings, database_url: str) -> Iterator[TestClient]:
    _seed(database_url)
    with TestClient(
        _app(settings), follow_redirects=False, headers=SAME_ORIGIN
    ) as test_client:
        yield test_client


def _sign_in(client: TestClient, email: str = "alice@example.com") -> None:
    client.cookies.clear()
    response = client.post("/login", data={"email": email, "password": PASSWORD})
    assert response.status_code == 303


def _change(
    client: TestClient,
    current: str = PASSWORD,
    new: str = NEW_PASSWORD,
    confirm: str | None = None,
) -> object:
    return client.post(
        "/account/password",
        data={
            "current_password": current,
            "new_password": new,
            "confirm_password": new if confirm is None else confirm,
        },
    )


def _can_sign_in(client: TestClient, email: str, password: str) -> bool:
    client.cookies.clear()
    return (
        client.post("/login", data={"email": email, "password": password}).status_code
        == 303
    )


def test_the_page_and_the_change_need_a_sign_in(client: TestClient) -> None:
    assert client.get("/account/password").status_code == 303
    assert _change(client).status_code == 303  # type: ignore[attr-defined]


def test_the_form_renders_for_a_signed_in_user(client: TestClient) -> None:
    _sign_in(client)

    page = client.get("/account/password")

    assert page.status_code == 200
    assert 'name="current_password"' in page.text
    assert "Password changed" not in page.text


def test_changing_the_password_replaces_it(client: TestClient) -> None:
    _sign_in(client)

    response = _change(client)

    assert response.status_code == 303  # type: ignore[attr-defined]
    assert "Password changed" in client.get("/account/password?done=true").text
    assert not _can_sign_in(client, "alice@example.com", PASSWORD)
    assert _can_sign_in(client, "alice@example.com", NEW_PASSWORD)


def test_it_ends_every_other_session_but_keeps_this_one(
    client: TestClient, database_url: str
) -> None:
    _sign_in(client)
    other_browser = dict(client.cookies)
    _sign_in(client)
    this_browser = dict(client.cookies)
    assert _count(database_url, "sessions", "alice@example.com") == 2

    assert _change(client).status_code == 303  # type: ignore[attr-defined]

    assert _count(database_url, "sessions", "alice@example.com") == 1
    assert client.get("/whoami").status_code == 200
    client.cookies.clear()
    client.cookies.update(other_browser)
    assert client.get("/whoami").status_code == 303
    client.cookies.clear()
    client.cookies.update(this_browser)
    assert client.get("/whoami").status_code == 200


def test_it_forgets_the_remembered_browsers(
    client: TestClient, database_url: str
) -> None:
    _sign_in(client)
    assert _count(database_url, "devices", "alice@example.com") == 1

    _change(client)

    assert _count(database_url, "devices", "alice@example.com") == 0


def test_it_leaves_other_users_alone(client: TestClient, database_url: str) -> None:
    _sign_in(client, "bob@example.com")
    bob = dict(client.cookies)
    _sign_in(client)

    _change(client)

    assert _count(database_url, "devices", "bob@example.com") == 1
    client.cookies.clear()
    client.cookies.update(bob)
    assert client.get("/whoami").json() == {"email": "bob@example.com"}


@pytest.mark.parametrize(
    ("current", "new", "confirm", "message"),
    [
        ("wrong password here", NEW_PASSWORD, None, "current password is incorrect"),
        (PASSWORD, NEW_PASSWORD, "something else entirely", "do not match"),
        (PASSWORD, "short", None, "at least 12 characters"),
        (PASSWORD, PASSWORD, None, "different password"),
    ],
)
def test_a_refused_change_says_why_and_changes_nothing(
    client: TestClient,
    database_url: str,
    current: str,
    new: str,
    confirm: str | None,
    message: str,
) -> None:
    _sign_in(client)
    other_browser = dict(client.cookies)
    _sign_in(client)

    response = _change(client, current, new, confirm)

    assert response.status_code == 400  # type: ignore[attr-defined]
    assert message in response.text  # type: ignore[attr-defined]
    assert _count(database_url, "sessions", "alice@example.com") == 2
    assert _count(database_url, "devices", "alice@example.com") == 2
    client.cookies.clear()
    client.cookies.update(other_browser)
    assert client.get("/whoami").status_code == 200
    assert _can_sign_in(client, "alice@example.com", PASSWORD)


def test_guessing_the_current_password_is_throttled(client: TestClient) -> None:
    _sign_in(client)

    statuses = [
        _change(client, current=f"guess number {n:02d} here").status_code  # type: ignore[attr-defined]
        for n in range(7)
    ]

    assert statuses[:5] == [400] * 5
    assert statuses[5:] == [429, 429]
    refused = _change(client)
    assert refused.status_code == 429  # type: ignore[attr-defined]
    assert "Too many failed attempts" in refused.text  # type: ignore[attr-defined]
    assert int(refused.headers["Retry-After"]) > 0  # type: ignore[attr-defined]


async def test_with_no_session_to_keep_every_session_ends(
    db_session: AsyncSession,
) -> None:
    auth = AuthService(db_session, session_ttl=timedelta(hours=1))
    user = await auth.create_user(
        email="a@example.com", display_name="A", password=PASSWORD
    )
    await auth.sign_in("a@example.com", PASSWORD)
    await auth.sign_in("a@example.com", PASSWORD)

    await auth.change_password(user, PASSWORD, NEW_PASSWORD, keep_token=None)

    assert await db_session.scalar(select(func.count(AuthSession.id))) == 0
