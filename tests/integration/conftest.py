from typing import TYPE_CHECKING

import pytest

import casttrail.auth.models  # noqa: F401
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
