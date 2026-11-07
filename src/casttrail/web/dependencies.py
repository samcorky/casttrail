from datetime import timedelta
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from casttrail.auth.models import User
from casttrail.auth.service import AuthService
from casttrail.config import Settings
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
    return AuthService(session, session_ttl=timedelta(hours=settings.session_ttl_hours))


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
