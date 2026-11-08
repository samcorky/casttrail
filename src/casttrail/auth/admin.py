from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from casttrail.auth.errors import AccountError
from casttrail.auth.models import AuthSession, KnownDevice, User

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


class AdminService:
    """What an administrator can do to other accounts.

    Creating an account is ``AuthService.create_user``. There is deliberately no way
    to remove admin rights or to disable yourself, so the last active administrator
    cannot be locked out by the pages built on this.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Create the service.

        Args:
            session: The database session to work in.
        """
        self._session = session

    async def list_users(self) -> list[User]:
        """Return every account, oldest first.

        Returns:
            All users.
        """
        result = await self._session.scalars(
            select(User).order_by(User.created_at, User.email)
        )
        return list(result)

    async def set_active(
        self, actor: User, user_id: uuid.UUID, *, active: bool
    ) -> User | None:
        """Enable or disable an account.

        Disabling also ends the account's sessions and forgets its devices, so it
        takes effect at once and nothing from before survives a later re-enable.

        Args:
            actor: The administrator making the change.
            user_id: The account to change.
            active: Whether the account should be able to sign in.

        Returns:
            The updated user, or ``None`` if there is no such account.

        Raises:
            AccountError: If an administrator tries to disable themselves.
        """
        if not active and user_id == actor.id:
            msg = "You cannot disable your own account."
            raise AccountError(msg)
        user = await self._session.get(User, user_id)
        if user is None:
            return None
        user.is_active = active
        if not active:
            await self._session.execute(
                delete(AuthSession).where(AuthSession.user_id == user.id)
            )
            await self._session.execute(
                delete(KnownDevice).where(KnownDevice.user_id == user.id)
            )
        await self._session.commit()
        return user

    async def make_admin(self, user_id: uuid.UUID) -> User | None:
        """Give an account administrator rights.

        Args:
            user_id: The account to promote.

        Returns:
            The updated user, or ``None`` if there is no such account.
        """
        user = await self._session.get(User, user_id)
        if user is None:
            return None
        user.is_admin = True
        await self._session.commit()
        return user
