import asyncio
from logging.config import fileConfig
from typing import TYPE_CHECKING

from alembic import context

# Importing each models module registers its tables on Base.metadata.
import casttrail.auth.models
import casttrail.catalogue.models
import casttrail.identity.models
import casttrail.watching.models  # noqa: F401
from casttrail.config import Settings
from casttrail.db import Base, create_engine

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    """Prefer an explicit URL (tests, scripts), else the application settings."""
    return config.get_main_option("sqlalchemy.url") or Settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL without connecting."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite needs table rebuilds for most ALTERs.
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations through the same engine configuration the app uses."""
    engine = create_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
