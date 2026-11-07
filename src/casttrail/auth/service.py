from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from casttrail.auth.models import AuthSession, User
from casttrail.auth.passwords import DUMMY_HASH, hash_password, verify_password
from casttrail.auth.throttle import SignInThrottle, ThrottlePolicy
from casttrail.auth.tokens import hash_token, new_token
from casttrail.db import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime, timedelta

    from sqlalchemy.ext.asyncio import AsyncSession


def normalise_email(email: str) -> str:
    """Return the form of an email address used for storage and lookup.

    Args:
        email: The address as typed.

    Returns:
        The address trimmed and lower-cased.
    """
    return email.strip().lower()


@dataclass(frozen=True, kw_only=True)
class SignedIn:
    """A user and the secret token of the session just started for them."""

    user: User
    token: str = field(repr=False)


class AuthService:
    """Account creation, sign-in, session lookup and sign-out."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        session_ttl: timedelta,
        throttle_policy: ThrottlePolicy | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        """Create the service.

        Args:
            session: The database session to work in.
            session_ttl: How long a new session lasts.
            throttle_policy: Limits on failed sign-ins. Defaults are used if omitted.
            now: The clock, replaceable in tests.
        """
        self._session = session
        self._ttl = session_ttl
        self._policy = throttle_policy or ThrottlePolicy()
        self._now = now

    async def create_user(
        self,
        *,
        email: str,
        display_name: str,
        password: str,
        is_admin: bool = False,
    ) -> User:
        """Create an account.

        Args:
            email: The login address.
            display_name: The name shown in the interface.
            password: The plain-text password.
            is_admin: Whether the account can administer the app.

        Returns:
            The stored user.
        """
        user = User(
            email=normalise_email(email),
            display_name=display_name.strip(),
            password_hash=hash_password(password),
            is_admin=is_admin,
        )
        self._session.add(user)
        await self._session.commit()
        return user

    async def sign_in(
        self,
        email: str,
        password: str,
        *,
        user_agent: str | None = None,
        client_ip: str | None = None,
    ) -> SignedIn | None:
        """Check credentials and start a session.

        An unknown email, a wrong password and a disabled account are
        indistinguishable to the caller, in both result and timing. Failures are
        counted per email and per address; once either is over its limit, attempts
        are refused before the password is even checked.

        Args:
            email: The address as typed.
            password: The plain-text password.
            user_agent: The browser's user agent, kept for the user's session list.
            client_ip: The client address.

        Returns:
            The user and session token, or ``None`` if sign-in is refused.

        Raises:
            SignInThrottledError: If there have been too many recent failures.
        """
        now = self._now()
        address = normalise_email(email)
        throttle = SignInThrottle(self._session, self._policy, now)
        await throttle.check(address, client_ip)

        user = await self._session.scalar(select(User).where(User.email == address))
        if user is None:
            verify_password(password, DUMMY_HASH)
        if (
            user is None
            or not verify_password(password, user.password_hash)
            or not user.is_active
        ):
            await throttle.record_failure(address, client_ip)
            await self._session.commit()
            return None

        await throttle.clear(address)
        token = new_token()
        self._session.add(
            AuthSession(
                user_id=user.id,
                token_hash=hash_token(token),
                created_at=now,
                expires_at=now + self._ttl,
                user_agent=user_agent[:255] if user_agent else None,
                client_ip=client_ip,
            )
        )
        await self._session.commit()
        return SignedIn(user=user, token=token)

    async def authenticate(self, token: str) -> User | None:
        """Find the active user a session token belongs to.

        Args:
            token: The secret token from the cookie.

        Returns:
            The user, or ``None`` if the session is unknown or expired, or the
            account is disabled.
        """
        row = await self._session.execute(
            select(User)
            .join(AuthSession, AuthSession.user_id == User.id)
            .where(
                AuthSession.token_hash == hash_token(token),
                AuthSession.expires_at > self._now(),
                User.is_active.is_(True),
            )
        )
        return row.scalar_one_or_none()

    async def sign_out(self, token: str) -> None:
        """End the session a token belongs to.

        Args:
            token: The secret token from the cookie.
        """
        await self._session.execute(
            delete(AuthSession).where(AuthSession.token_hash == hash_token(token))
        )
        await self._session.commit()
