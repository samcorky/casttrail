from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from starlette.testclient import TestClient

from casttrail.auth.bootstrap import bootstrap_admin
from casttrail.auth.models import User
from casttrail.auth.service import AuthService
from casttrail.config import Settings
from casttrail.main import create_app

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


def _with_admin(settings: Settings, email: str, password: str) -> Settings:
    return Settings(
        database_url=settings.database_url,
        admin_email=email,
        admin_password=SecretStr(password),
    )


@pytest.fixture
def auth(db_session: AsyncSession) -> AuthService:
    return AuthService(db_session, session_ttl=timedelta(hours=1))


async def test_the_first_administrator_is_created_on_an_empty_database(
    auth: AuthService, db_session: AsyncSession
) -> None:
    created = await bootstrap_admin(
        auth,
        db_session,
        email="Admin@Example.com",
        password=PASSWORD,
        display_name="Sam",
    )

    user = await db_session.scalar(select(User))
    assert created
    assert user is not None
    assert user.email == "admin@example.com"
    assert user.display_name == "Sam"
    assert user.is_admin
    assert user.is_active
    assert (await auth.sign_in("admin@example.com", PASSWORD)) is not None


async def test_nothing_is_created_once_any_user_exists(
    auth: AuthService, db_session: AsyncSession
) -> None:
    await auth.create_user(email="a@example.com", display_name="A", password=PASSWORD)

    created = await bootstrap_admin(
        auth, db_session, email="b@example.com", password=PASSWORD, display_name="B"
    )

    assert not created
    assert await db_session.scalar(select(func.count(User.id))) == 1


async def test_a_disabled_user_still_counts_so_the_env_cannot_add_an_admin(
    auth: AuthService, db_session: AsyncSession
) -> None:
    user = await auth.create_user(
        email="a@example.com", display_name="A", password=PASSWORD
    )
    user.is_active = False
    await db_session.commit()

    created = await bootstrap_admin(
        auth, db_session, email="b@example.com", password=PASSWORD, display_name="B"
    )

    assert not created


def test_startup_creates_the_admin_once_and_never_resets_it(
    migrated_settings: Settings,
) -> None:
    first = _with_admin(migrated_settings, "admin@example.com", PASSWORD)
    with TestClient(create_app(first)):
        pass

    second = _with_admin(migrated_settings, "admin@example.com", "a different password")
    with TestClient(
        create_app(second), follow_redirects=False, headers=SAME_ORIGIN
    ) as client:
        old = client.post(
            "/login", data={"email": "admin@example.com", "password": PASSWORD}
        )
        new = client.post(
            "/login",
            data={"email": "admin@example.com", "password": "a different password"},
        )

    assert old.status_code == 303
    assert new.status_code == 401


def test_startup_without_admin_settings_creates_nobody(
    migrated_settings: Settings,
) -> None:
    with TestClient(create_app(migrated_settings)):
        pass

    # A second start still works and the database stays empty of users.
    with TestClient(create_app(migrated_settings)) as client:
        response = client.get("/login")
    assert response.status_code == 200


def test_startup_with_admin_settings_and_no_tables_explains_what_to_do(
    settings: Settings,
) -> None:
    configured = _with_admin(settings, "admin@example.com", PASSWORD)

    with (
        pytest.raises(RuntimeError, match="poe migrate"),
        TestClient(create_app(configured)),
    ):
        pass
