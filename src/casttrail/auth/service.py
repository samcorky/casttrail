import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from casttrail.auth.errors import AccountError
from casttrail.auth.models import AuthSession, KnownDevice, User
from casttrail.auth.passwords import (
    DUMMY_HASH,
    check_new_password,
    hash_password,
    verify_password,
)
from casttrail.auth.throttle import SignInThrottle, ThrottlePolicy
from casttrail.auth.tokens import hash_token, new_token
from casttrail.db import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime, timedelta

    from sqlalchemy.ext.asyncio import AsyncSession

MAX_EMAIL_LENGTH = 320
MAX_NAME_LENGTH = 100

# Looked up when the email is unknown, so an unknown email runs the same queries as a
# real one and cannot be told apart by what the server does.
_NO_USER = uuid.UUID(int=0)


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
    """A user and the secret tokens of the session just started for them."""

    user: User
    token: str = field(repr=False)
    # The long-lived device cookie value to (re)set in the browser.
    device_token: str = field(repr=False)


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

        Raises:
            AccountError: If the email, name or password is not acceptable, or the
                email is already in use.
        """
        address = normalise_email(email)
        name = display_name.strip()
        if "@" not in address or len(address) > MAX_EMAIL_LENGTH:
            msg = "Enter a valid email address."
            raise AccountError(msg)
        if not name or len(name) > MAX_NAME_LENGTH:
            msg = f"The name must be 1 to {MAX_NAME_LENGTH} characters."
            raise AccountError(msg)
        check_new_password(password)
        user = User(
            email=address,
            display_name=name,
            password_hash=hash_password(password),
            is_admin=is_admin,
        )
        self._session.add(user)
        try:
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            msg = "That email address is already in use."
            raise AccountError(msg) from error
        return user

    async def sign_in(
        self,
        email: str,
        password: str,
        *,
        user_agent: str | None = None,
        client_ip: str | None = None,
        device_token: str | None = None,
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
            device_token: The device cookie the browser sent, if any.

        Returns:
            The user and tokens, or ``None`` if sign-in is refused.

        Raises:
            SignInThrottledError: If there have been too many recent failures.
        """
        now = self._now()
        address = normalise_email(email)
        throttle = SignInThrottle(self._session, self._policy, now)
        # The check, the password verification and the failure record share one
        # transaction. It begins IMMEDIATE (see casttrail.db), so concurrent attempts
        # queue up here and each sees the previous one's failure. Without that, a burst
        # could all pass the check before any of them recorded a failure
        # (tests/integration/test_signin_throttle.py proves this).
        user = await self._session.scalar(select(User).where(User.email == address))
        # A browser holding this account's device cookie gets a higher, still finite,
        # per-email limit, so someone else's guesses cannot easily lock the real user
        # out. The lookup runs for unknown emails too, so it reveals nothing.
        known_device = await self._is_known_device(
            user.id if user else _NO_USER, device_token
        )
        await throttle.check(address, client_ip, known_device=known_device)

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
        await self._session.execute(
            delete(AuthSession).where(AuthSession.expires_at < now)
        )
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
        if known_device and device_token is not None:
            device = device_token
        else:
            device = new_token()
            self._session.add(
                KnownDevice(
                    user_id=user.id, token_hash=hash_token(device), created_at=now
                )
            )
        await self._session.commit()
        return SignedIn(user=user, token=token, device_token=device)

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

        The device cookie is left alone: signing out is not forgetting the browser.

        Args:
            token: The secret token from the cookie.
        """
        await self._session.execute(
            delete(AuthSession).where(AuthSession.token_hash == hash_token(token))
        )
        await self._session.commit()

    async def forget_devices(self, user_id: uuid.UUID) -> None:
        """Forget every remembered browser for an account.

        To be called when the password changes or is reset, or the account's sessions
        are revoked, so a device cookie from before cannot keep its higher limit.

        Args:
            user_id: The account.
        """
        await self._session.execute(
            delete(KnownDevice).where(KnownDevice.user_id == user_id)
        )
        await self._session.commit()

    async def _is_known_device(
        self, user_id: uuid.UUID, device_token: str | None
    ) -> bool:
        if device_token is None:
            return False
        found = await self._session.scalar(
            select(KnownDevice.id).where(
                KnownDevice.user_id == user_id,
                KnownDevice.token_hash == hash_token(device_token),
            )
        )
        return found is not None
