import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select

from casttrail.auth.models import SignInAttempt

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ThrottlePolicy:
    """How many failed sign-ins are tolerated before refusing further attempts."""

    max_failures_per_user: int = 5
    # The per-email limit for a browser that has signed in to the account before.
    # Higher, but finite, so a stolen device cookie is not a free pass.
    max_failures_per_user_known_device: int = 20
    max_failures_per_ip: int = 20
    window: timedelta = timedelta(minutes=15)


class SignInThrottledError(Exception):
    """Raised when too many recent sign-ins have failed."""

    def __init__(self, retry_after: timedelta) -> None:
        """Create the error.

        Args:
            retry_after: An upper bound on how long until attempts are allowed again.
        """
        super().__init__("Too many failed sign-in attempts.")
        self.retry_after = retry_after


def _user_key(email: str) -> str:
    return _digest(f"user:{email}")


def _ip_key(client_ip: str) -> str:
    return _digest(f"ip:{client_ip}")


def _digest(value: str) -> str:
    return hashlib.sha256(value.lower().encode()).hexdigest()


class SignInThrottle:
    """Counts failed sign-ins per email and per client address.

    Keys are hashed, so the table holds no readable emails or addresses. Unknown
    emails are counted exactly like real ones, so the throttle reveals nothing
    about which accounts exist.
    """

    def __init__(
        self,
        session: AsyncSession,
        policy: ThrottlePolicy,
        now: datetime,
    ) -> None:
        """Create the throttle.

        Args:
            session: The database session to work in.
            policy: The limits to enforce.
            now: The current time.
        """
        self._session = session
        self._policy = policy
        self._now = now

    async def _failures(self, key: str) -> int:
        cutoff = self._now - self._policy.window
        count = await self._session.scalar(
            select(func.count())
            .select_from(SignInAttempt)
            .where(SignInAttempt.key_hash == key, SignInAttempt.attempted_at > cutoff)
        )
        return count or 0

    async def check(
        self,
        email: str,
        client_ip: str | None,
        *,
        known_device: bool = False,
    ) -> None:
        """Refuse the attempt if either counter is over its limit.

        Args:
            email: The normalised email being tried.
            client_ip: The client address, if known.
            known_device: Whether the browser holds a valid device cookie for this
                account, which raises the per-email limit.

        Raises:
            SignInThrottledError: If the email or the address has too many recent
                failures.
        """
        await self._session.execute(
            delete(SignInAttempt).where(
                SignInAttempt.attempted_at <= self._now - self._policy.window
            )
        )
        user_limit = (
            self._policy.max_failures_per_user_known_device
            if known_device
            else self._policy.max_failures_per_user
        )
        over_user = await self._failures(_user_key(email)) >= user_limit
        over_ip = (
            client_ip is not None
            and await self._failures(_ip_key(client_ip))
            >= self._policy.max_failures_per_ip
        )
        if over_user or over_ip:
            raise SignInThrottledError(self._policy.window)

    async def record_failure(self, email: str, client_ip: str | None) -> None:
        """Count a failed attempt against the email and the address.

        Args:
            email: The normalised email that was tried.
            client_ip: The client address, if known.
        """
        keys = [_user_key(email)]
        if client_ip is not None:
            keys.append(_ip_key(client_ip))
        for key in keys:
            self._session.add(SignInAttempt(key_hash=key, attempted_at=self._now))

    async def clear(self, email: str) -> None:
        """Forget an email's failures after a successful sign-in.

        The address's count is kept: one good login must not reset the budget a
        client has spent guessing other accounts.

        Args:
            email: The normalised email that signed in.
        """
        await self._session.execute(
            delete(SignInAttempt).where(SignInAttempt.key_hash == _user_key(email))
        )
