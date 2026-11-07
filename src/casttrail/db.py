from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, MetaData, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Dialect
    from sqlalchemy.pool import ConnectionPoolEntry

# Stable constraint names so Alembic batch migrations on SQLite can address them.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every persisted model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime.

    Returns:
        The current moment in UTC.
    """
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp stored as UTC and always returned timezone-aware.

    SQLite has no timezone support and would hand back naive values, which then
    cannot be compared with aware ones.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        """Convert an aware datetime to naive UTC for storage.

        Args:
            value: The value being stored.
            dialect: The database dialect.

        Returns:
            The naive UTC datetime, or ``None``.

        Raises:
            ValueError: If the datetime has no timezone.
        """
        if value is None:
            return None
        if value.tzinfo is None:
            msg = "Timestamps must be timezone-aware"
            raise ValueError(msg)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        """Attach UTC to a stored value.

        Args:
            value: The value read from the database.
            dialect: The database dialect.

        Returns:
            The aware UTC datetime, or ``None``.
        """
        return None if value is None else value.replace(tzinfo=UTC)


def _configure_connection(
    dbapi_connection: Any,  # noqa: ANN401 - driver-specific connection object
    _record: ConnectionPoolEntry,
) -> None:
    """Apply per-connection SQLite settings.

    Setting ``isolation_level`` to ``None`` stops the driver issuing its own implicit
    ``BEGIN``, so ``_begin_immediate`` controls how transactions start.
    """
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA synchronous = NORMAL")
    cursor.execute("PRAGMA busy_timeout = 5000")
    cursor.close()


def _begin_immediate(connection: Connection) -> None:
    """Take the write lock when a transaction starts.

    A deferred ``BEGIN`` that reads and then writes can fail at once with
    ``SQLITE_BUSY`` when another writer got in between, and the busy timeout does not
    help with that lock upgrade. Every transaction is ``IMMEDIATE`` instead, which
    queues writers behind the timeout. With two or three users, losing concurrent
    readers is not worth the complexity of a second engine.
    """
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def create_engine(url: str) -> AsyncEngine:
    """Create the async SQLite engine.

    Args:
        url: A ``sqlite+aiosqlite`` URL. The parent directory is created if needed.

    Returns:
        An engine configured for WAL mode with foreign keys enforced.
    """
    database = make_url(url).database
    if database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(url)
    event.listen(engine.sync_engine, "connect", _configure_connection)
    event.listen(engine.sync_engine, "begin", _begin_immediate)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create the session factory.

    Args:
        engine: The engine sessions are bound to.

    Returns:
        A factory that does not expire objects on commit.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


async def current_revision(session: AsyncSession) -> str | None:
    """Return the applied Alembic revision.

    Args:
        session: An open session.

    Returns:
        The revision identifier, or ``None`` when migrations have never been run.
    """
    try:
        result = await session.execute(text("SELECT version_num FROM alembic_version"))
    except OperationalError:
        return None
    return result.scalar_one_or_none()
