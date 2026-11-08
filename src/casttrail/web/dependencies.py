from datetime import timedelta
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from casttrail.auth.admin import AdminService
from casttrail.auth.models import User
from casttrail.auth.service import AuthService
from casttrail.auth.throttle import ThrottlePolicy
from casttrail.config import Settings
from casttrail.providers.base import MetadataProvider
from casttrail.watching.service import WatchService
from casttrail.web.cookies import read_session_token

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class LoginRequiredError(Exception):
    """Raised when a page needs a signed-in user and there is none."""


def get_settings(request: Request) -> Settings:
    """Return the settings the app was built with.

    Args:
        request: The current request.

    Returns:
        The application settings.
    """
    settings: Settings = request.app.state.settings
    return settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Provide a database session for the duration of a request.

    Args:
        request: The current request.

    Yields:
        An open session.
    """
    async with request.app.state.session_factory() as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_auth_service(session: SessionDep, settings: SettingsDep) -> AuthService:
    """Build the auth service for a request.

    Args:
        session: The request's database session.
        settings: The application settings.

    Returns:
        An auth service bound to the session.
    """
    return AuthService(
        session,
        session_ttl=timedelta(hours=settings.session_ttl_hours),
        throttle_policy=ThrottlePolicy(
            max_failures_per_user=settings.signin_max_failures_per_user,
            max_failures_per_user_known_device=(
                settings.signin_max_failures_per_user_known_device
            ),
            max_failures_per_ip=settings.signin_max_failures_per_ip,
            window=timedelta(minutes=settings.signin_window_minutes),
        ),
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


async def get_current_user(
    request: Request, settings: SettingsDep, auth: AuthServiceDep
) -> User | None:
    """Return the signed-in user, if any.

    Args:
        request: The current request.
        settings: The application settings.
        auth: The auth service.

    Returns:
        The user, or ``None`` when there is no valid session.
    """
    token = read_session_token(request, settings)
    if token is None:
        return None
    return await auth.authenticate(token)


CurrentUserOptional = Annotated[User | None, Depends(get_current_user)]


async def require_user(user: CurrentUserOptional) -> User:
    """Return the signed-in user or refuse the request.

    Args:
        user: The signed-in user, if any.

    Returns:
        The signed-in user.

    Raises:
        LoginRequiredError: If nobody is signed in.
    """
    if user is None:
        raise LoginRequiredError
    return user


CurrentUser = Annotated[User, Depends(require_user)]


async def require_admin(user: CurrentUser) -> User:
    """Return the signed-in administrator, or pretend the page does not exist.

    Args:
        user: The signed-in user.

    Returns:
        The user, who is an administrator.

    Raises:
        HTTPException: A 404 if the user is not an administrator.
    """
    if not user.is_admin:
        raise HTTPException(status_code=404)
    return user


CurrentAdmin = Annotated[User, Depends(require_admin)]


def get_admin_service(session: SessionDep) -> AdminService:
    """Build the admin service for a request.

    Args:
        session: The request's database session.

    Returns:
        An admin service bound to the session.
    """
    return AdminService(session)


AdminServiceDep = Annotated[AdminService, Depends(get_admin_service)]


def get_metadata_provider(request: Request) -> MetadataProvider | None:
    """Return where people, films and series come from, if it is set up.

    Args:
        request: The current request.

    Returns:
        The provider, or ``None`` when TMDB has no token configured.
    """
    provider: MetadataProvider | None = request.app.state.metadata
    return provider


MetadataDep = Annotated[MetadataProvider | None, Depends(get_metadata_provider)]


def get_watch_service(session: SessionDep) -> WatchService:
    """Build the watch service for a request.

    Args:
        session: The request's database session.

    Returns:
        A watch service bound to the session.
    """
    return WatchService(session)


WatchServiceDep = Annotated[WatchService, Depends(get_watch_service)]
