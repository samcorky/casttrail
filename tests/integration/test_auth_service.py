from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select, text

from casttrail.auth.errors import AccountError
from casttrail.auth.models import AuthSession, User
from casttrail.auth.service import AuthService
from casttrail.auth.tokens import hash_token

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
START = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
TTL = timedelta(hours=1)


class Clock:
    def __init__(self) -> None:
        self.current = START

    def __call__(self) -> datetime:
        return self.current


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def auth(db_session: AsyncSession, clock: Clock) -> AuthService:
    return AuthService(db_session, session_ttl=TTL, now=clock)


async def test_create_user_normalises_the_email_and_hashes_the_password(
    auth: AuthService,
) -> None:
    user = await auth.create_user(
        email="  Sam@Example.com ", display_name=" Sam ", password=PASSWORD
    )

    assert user.email == "sam@example.com"
    assert user.display_name == "Sam"
    assert user.password_hash != PASSWORD
    assert user.is_active
    assert not user.is_admin


async def test_emails_are_unique_regardless_of_case(auth: AuthService) -> None:
    await auth.create_user(email="a@example.com", display_name="A", password=PASSWORD)

    with pytest.raises(AccountError, match="already in use"):
        await auth.create_user(
            email="A@EXAMPLE.COM", display_name="B", password=PASSWORD
        )


async def test_sign_in_starts_a_session_that_authenticates(auth: AuthService) -> None:
    user = await auth.create_user(
        email="a@example.com", display_name="A", password=PASSWORD
    )

    signed_in = await auth.sign_in(" A@Example.com", PASSWORD)

    assert signed_in is not None
    assert signed_in.user.id == user.id
    authenticated = await auth.authenticate(signed_in.token)
    assert authenticated is not None
    assert authenticated.id == user.id


async def test_the_database_stores_only_the_token_hash(
    auth: AuthService, db_session: AsyncSession
) -> None:
    await auth.create_user(email="a@example.com", display_name="A", password=PASSWORD)
    signed_in = await auth.sign_in("a@example.com", PASSWORD)
    assert signed_in is not None

    stored = (await db_session.scalars(select(AuthSession))).one()

    assert stored.token_hash == hash_token(signed_in.token)
    assert signed_in.token not in stored.token_hash


async def test_session_records_the_client_and_truncates_the_user_agent(
    auth: AuthService, db_session: AsyncSession
) -> None:
    await auth.create_user(email="a@example.com", display_name="A", password=PASSWORD)

    await auth.sign_in(
        "a@example.com", PASSWORD, user_agent="x" * 1000, client_ip="203.0.113.9"
    )

    stored = (await db_session.scalars(select(AuthSession))).one()
    assert stored.user_agent == "x" * 255
    assert stored.client_ip == "203.0.113.9"


async def test_wrong_password_unknown_email_and_disabled_account_all_refuse(
    auth: AuthService, db_session: AsyncSession
) -> None:
    await auth.create_user(email="a@example.com", display_name="A", password=PASSWORD)
    disabled = await auth.create_user(
        email="off@example.com", display_name="Off", password=PASSWORD
    )
    disabled.is_active = False
    await db_session.commit()

    assert await auth.sign_in("a@example.com", "wrong") is None
    assert await auth.sign_in("nobody@example.com", PASSWORD) is None
    assert await auth.sign_in("off@example.com", PASSWORD) is None
    assert (await db_session.scalars(select(AuthSession))).all() == []


async def test_an_unknown_token_does_not_authenticate(auth: AuthService) -> None:
    assert await auth.authenticate("not-a-real-token") is None


async def test_a_session_expires(auth: AuthService, clock: Clock) -> None:
    await auth.create_user(email="a@example.com", display_name="A", password=PASSWORD)
    signed_in = await auth.sign_in("a@example.com", PASSWORD)
    assert signed_in is not None

    clock.current = START + TTL - timedelta(seconds=1)
    assert await auth.authenticate(signed_in.token) is not None

    clock.current = START + TTL
    assert await auth.authenticate(signed_in.token) is None


async def test_disabling_a_user_ends_access_on_the_next_request(
    auth: AuthService, db_session: AsyncSession
) -> None:
    user = await auth.create_user(
        email="a@example.com", display_name="A", password=PASSWORD
    )
    signed_in = await auth.sign_in("a@example.com", PASSWORD)
    assert signed_in is not None
    assert await auth.authenticate(signed_in.token) is not None

    user.is_active = False
    await db_session.commit()

    assert await auth.authenticate(signed_in.token) is None


async def test_sign_out_ends_only_that_session(auth: AuthService) -> None:
    await auth.create_user(email="a@example.com", display_name="A", password=PASSWORD)
    first = await auth.sign_in("a@example.com", PASSWORD)
    second = await auth.sign_in("a@example.com", PASSWORD)
    assert first is not None
    assert second is not None

    await auth.sign_out(first.token)

    assert await auth.authenticate(first.token) is None
    assert await auth.authenticate(second.token) is not None


async def test_two_users_have_independent_sessions(auth: AuthService) -> None:
    await auth.create_user(email="a@example.com", display_name="A", password=PASSWORD)
    await auth.create_user(email="b@example.com", display_name="B", password=PASSWORD)
    a = await auth.sign_in("a@example.com", PASSWORD)
    b = await auth.sign_in("b@example.com", PASSWORD)
    assert a is not None
    assert b is not None

    user_a = await auth.authenticate(a.token)
    user_b = await auth.authenticate(b.token)

    assert user_a is not None
    assert user_b is not None
    assert user_a.email == "a@example.com"
    assert user_b.email == "b@example.com"


async def test_deleting_a_user_deletes_their_sessions(
    auth: AuthService, db_session: AsyncSession
) -> None:
    user = await auth.create_user(
        email="a@example.com", display_name="A", password=PASSWORD
    )
    await auth.sign_in("a@example.com", PASSWORD)

    await db_session.delete(user)
    await db_session.commit()

    remaining = await db_session.scalar(text("SELECT count(*) FROM sessions"))
    assert remaining == 0
    assert (await db_session.scalars(select(User))).all() == []
