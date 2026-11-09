from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config

import casttrail.auth.models
import casttrail.catalogue.models
import casttrail.discovery.models
import casttrail.identity.models
import casttrail.matching.models
import casttrail.watching.models  # noqa: F401
from casttrail.config import Settings  # noqa: TC001  (pytest resolves fixture types)
from casttrail.db import Base, create_engine, create_session_factory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
async def db_session(database_url: str) -> AsyncIterator[AsyncSession]:
    """A session on a fresh database with the current models' tables.

    The migrations are checked against the models by the pytest-alembic tests, so
    service tests build the schema directly and stay fast.
    """
    engine = create_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with create_session_factory(engine)() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def migrated_settings(settings: Settings, database_url: str) -> Settings:
    """Settings for a database that has had every migration applied."""
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    return settings
