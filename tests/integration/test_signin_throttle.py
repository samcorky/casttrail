import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from casttrail.auth.models import SignInAttempt
from casttrail.auth.service import AuthService
from casttrail.auth.throttle import SignInThrottledError, ThrottlePolicy
from casttrail.db import Base, create_engine, create_session_factory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"
START = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
POLICY = ThrottlePolicy(
    max_failures_per_user=3, max_failures_per_ip=5, window=timedelta(minutes=10)
)


class Clock:
    def __init__(self) -> None:
        self.current = START

    def __call__(self) -> datetime:
        return self.current


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def auth(db_session: AsyncSession, clock: Clock) -> AuthService:
    service = AuthService(
        db_session,
        session_ttl=timedelta(hours=1),
        throttle_policy=POLICY,
        now=clock,
    )
    await service.create_user(
        email="a@example.com", display_name="A", password=PASSWORD
    )
    return service


async def _fail(auth: AuthService, times: int, *, email: str = "a@example.com") -> None:
    for _ in range(times):
        assert await auth.sign_in(email, "wrong", client_ip="203.0.113.1") is None


async def test_attempts_up_to_the_limit_are_still_checked_normally(
    auth: AuthService,
) -> None:
    await _fail(auth, 3 - 1)

    assert await auth.sign_in("a@example.com", PASSWORD) is not None


async def test_too_many_failures_block_even_the_correct_password(
    auth: AuthService,
) -> None:
    await _fail(auth, 3)

    with pytest.raises(SignInThrottledError) as blocked:
        await auth.sign_in("a@example.com", PASSWORD)

    assert blocked.value.retry_after == POLICY.window


async def test_the_block_lifts_when_the_window_passes(
    auth: AuthService, clock: Clock
) -> None:
    await _fail(auth, 3)
    clock.current = START + POLICY.window - timedelta(seconds=1)
    with pytest.raises(SignInThrottledError):
        await auth.sign_in("a@example.com", PASSWORD)

    clock.current = START + POLICY.window

    assert await auth.sign_in("a@example.com", PASSWORD) is not None


async def test_blocked_attempts_do_not_extend_the_block(
    auth: AuthService, clock: Clock, db_session: AsyncSession
) -> None:
    await _fail(auth, 3)
    clock.current = START + timedelta(minutes=5)
    for _ in range(4):
        with pytest.raises(SignInThrottledError):
            await auth.sign_in("a@example.com", "wrong")

    stored = await db_session.scalar(select(func.count()).select_from(SignInAttempt))
    assert stored == 6  # 3 failures for the email and 3 for the address; no more


async def test_a_success_clears_the_email_counter(auth: AuthService) -> None:
    await _fail(auth, 2)
    assert await auth.sign_in("a@example.com", PASSWORD) is not None

    await _fail(auth, 2)

    assert await auth.sign_in("a@example.com", PASSWORD) is not None


async def test_the_email_is_counted_regardless_of_case_and_spacing(
    auth: AuthService,
) -> None:
    await auth.sign_in("a@example.com", "wrong")
    await auth.sign_in(" A@Example.COM ", "wrong")
    await auth.sign_in("a@EXAMPLE.com", "wrong")

    with pytest.raises(SignInThrottledError):
        await auth.sign_in("a@example.com", PASSWORD)


async def test_unknown_emails_are_throttled_just_like_real_ones(
    auth: AuthService,
) -> None:
    await _fail(auth, 3, email="nobody@example.com")

    with pytest.raises(SignInThrottledError):
        await auth.sign_in("nobody@example.com", PASSWORD)


async def test_one_address_trying_many_emails_is_blocked(auth: AuthService) -> None:
    for number in range(5):
        email = f"guess{number}@example.com"
        assert await auth.sign_in(email, "wrong", client_ip="198.51.100.7") is None

    with pytest.raises(SignInThrottledError):
        await auth.sign_in("a@example.com", PASSWORD, client_ip="198.51.100.7")


async def test_another_address_is_unaffected_by_that_block(auth: AuthService) -> None:
    for number in range(5):
        await auth.sign_in(
            f"guess{number}@example.com", "wrong", client_ip="198.51.100.7"
        )

    signed_in = await auth.sign_in("a@example.com", PASSWORD, client_ip="203.0.113.99")

    assert signed_in is not None


async def test_a_success_does_not_reset_the_address_counter(auth: AuthService) -> None:
    for number in range(4):
        await auth.sign_in(
            f"guess{number}@example.com", "wrong", client_ip="198.51.100.7"
        )
    assert await auth.sign_in("a@example.com", PASSWORD, client_ip="198.51.100.7")

    await auth.sign_in("guess9@example.com", "wrong", client_ip="198.51.100.7")

    with pytest.raises(SignInThrottledError):
        await auth.sign_in("a@example.com", PASSWORD, client_ip="198.51.100.7")


async def test_a_missing_address_still_throttles_by_email(auth: AuthService) -> None:
    for _ in range(3):
        await auth.sign_in("a@example.com", "wrong", client_ip=None)

    with pytest.raises(SignInThrottledError):
        await auth.sign_in("a@example.com", PASSWORD, client_ip=None)


async def test_old_attempts_are_pruned(
    auth: AuthService, clock: Clock, db_session: AsyncSession
) -> None:
    await _fail(auth, 2)
    clock.current = START + POLICY.window + timedelta(minutes=1)

    await auth.sign_in("a@example.com", PASSWORD)

    stored = await db_session.scalar(select(func.count()).select_from(SignInAttempt))
    assert stored == 0


async def test_stored_keys_are_hashes_not_emails_or_addresses(
    auth: AuthService, db_session: AsyncSession
) -> None:
    await auth.sign_in("a@example.com", "wrong", client_ip="203.0.113.1")

    keys = (await db_session.scalars(select(SignInAttempt.key_hash))).all()

    assert len(keys) == 2
    assert all(len(key) == 64 for key in keys)
    assert not any("example" in key or "203" in key for key in keys)


async def test_concurrent_attempts_cannot_overshoot_the_failure_limit(
    database_url: str,
) -> None:
    """Each sign-in checks, verifies and records in one write transaction.

    Every transaction begins ``IMMEDIATE``, so a second attempt waits for the first to
    commit before it counts failures. Without that, a burst could all pass the check
    and then all record a failure.
    """
    engine = create_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as session:
        await AuthService(session, session_ttl=timedelta(hours=1)).create_user(
            email="a@example.com", display_name="A", password=PASSWORD
        )

    async def attempt() -> str:
        async with factory() as session:
            service = AuthService(
                session, session_ttl=timedelta(hours=1), throttle_policy=POLICY
            )
            try:
                result = await service.sign_in("a@example.com", "wrong")
            except SignInThrottledError:
                return "throttled"
            return "refused" if result is None else "signed-in"

    try:
        outcomes = await asyncio.gather(*(attempt() for _ in range(10)))
    finally:
        await engine.dispose()

    assert outcomes.count("refused") == POLICY.max_failures_per_user
    assert outcomes.count("throttled") == 10 - POLICY.max_failures_per_user


HOME = "203.0.113.10"
ATTACKER = "198.51.100.66"


async def _lock_out_from_elsewhere(auth: AuthService) -> None:
    for _ in range(3):
        assert await auth.sign_in("a@example.com", "wrong", client_ip=ATTACKER) is None
    with pytest.raises(SignInThrottledError):
        await auth.sign_in("a@example.com", PASSWORD, client_ip=ATTACKER)


async def test_a_known_address_is_not_locked_out_by_guesses_from_elsewhere(
    auth: AuthService,
) -> None:
    assert await auth.sign_in("a@example.com", PASSWORD, client_ip=HOME) is not None

    await _lock_out_from_elsewhere(auth)

    assert await auth.sign_in("a@example.com", PASSWORD, client_ip=HOME) is not None


async def test_an_unknown_address_is_still_locked_out(auth: AuthService) -> None:
    await auth.sign_in("a@example.com", PASSWORD, client_ip=HOME)
    await _lock_out_from_elsewhere(auth)

    with pytest.raises(SignInThrottledError):
        await auth.sign_in("a@example.com", PASSWORD, client_ip="192.0.2.77")
    with pytest.raises(SignInThrottledError):
        await auth.sign_in("a@example.com", PASSWORD, client_ip=None)


async def test_a_known_address_is_still_limited_per_address(
    auth: AuthService,
) -> None:
    await auth.sign_in("a@example.com", PASSWORD, client_ip=HOME)
    for number in range(5):
        await auth.sign_in(f"guess{number}@example.com", "wrong", client_ip=HOME)

    with pytest.raises(SignInThrottledError):
        await auth.sign_in("a@example.com", PASSWORD, client_ip=HOME)


async def test_an_address_known_to_one_user_is_not_known_for_another(
    auth: AuthService,
) -> None:
    await auth.create_user(email="b@example.com", display_name="B", password=PASSWORD)
    await auth.sign_in("a@example.com", PASSWORD, client_ip=HOME)
    for _ in range(3):
        await auth.sign_in("b@example.com", "wrong", client_ip=ATTACKER)

    with pytest.raises(SignInThrottledError):
        await auth.sign_in("b@example.com", PASSWORD, client_ip=HOME)


async def test_signing_out_keeps_the_address_known(auth: AuthService) -> None:
    first = await auth.sign_in("a@example.com", PASSWORD, client_ip=HOME)
    assert first is not None
    await auth.sign_out(first.token)
    assert await auth.authenticate(first.token) is None

    await _lock_out_from_elsewhere(auth)

    assert await auth.sign_in("a@example.com", PASSWORD, client_ip=HOME) is not None


async def test_an_address_is_forgotten_after_the_retention_period(
    auth: AuthService, clock: Clock
) -> None:
    await auth.sign_in("a@example.com", PASSWORD, client_ip=HOME)
    await _lock_out_from_elsewhere(auth)

    clock.current = START + timedelta(days=200)
    await auth.create_user(email="c@example.com", display_name="C", password=PASSWORD)
    assert await auth.sign_in("c@example.com", PASSWORD) is not None  # runs the prune
    for _ in range(3):
        await auth.sign_in("a@example.com", "wrong", client_ip=ATTACKER)

    with pytest.raises(SignInThrottledError):
        await auth.sign_in("a@example.com", PASSWORD, client_ip=HOME)


async def test_signing_out_twice_is_harmless(auth: AuthService) -> None:
    signed_in = await auth.sign_in("a@example.com", PASSWORD)
    assert signed_in is not None

    await auth.sign_out(signed_in.token)
    await auth.sign_out(signed_in.token)

    assert await auth.authenticate(signed_in.token) is None
