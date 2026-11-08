from typing import TYPE_CHECKING

from sqlalchemy import select

from casttrail.auth.models import User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.auth.service import AuthService


async def bootstrap_admin(
    auth: AuthService,
    session: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str,
) -> bool:
    """Create the first administrator, but only on a database with no users.

    Once any account exists this does nothing, so the environment variables cannot
    be used later to add an admin or to reset a password.

    Args:
        auth: The auth service used to create the account.
        session: The session used to check whether any user exists.
        email: The administrator's login address.
        password: The administrator's plain-text password.
        display_name: The name shown in the interface.

    Returns:
        ``True`` if the administrator was created, ``False`` if users already exist.
    """
    if await session.scalar(select(User.id).limit(1)) is not None:
        return False
    await auth.create_user(
        email=email, display_name=display_name, password=password, is_admin=True
    )
    return True
