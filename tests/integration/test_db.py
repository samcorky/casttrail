from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, text

from casttrail.db import create_engine, create_session_factory, current_revision

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database_url)
    yield engine
    await engine.dispose()


async def test_connections_enforce_foreign_keys_and_use_wal(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        foreign_keys = await connection.scalar(text("PRAGMA foreign_keys"))
        journal_mode = await connection.scalar(text("PRAGMA journal_mode"))
        busy_timeout = await connection.scalar(text("PRAGMA busy_timeout"))

    assert foreign_keys == 1
    assert journal_mode == "wal"
    assert busy_timeout == 5000


async def test_every_transaction_begins_immediate(engine: AsyncEngine) -> None:
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def record(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        statements.append(statement)

    async with create_session_factory(engine)() as session:
        await session.execute(text("SELECT 1"))

    assert statements[0] == "BEGIN IMMEDIATE"


async def test_creates_the_database_directory(tmp_path: object) -> None:
    from pathlib import Path

    nested = Path(str(tmp_path)) / "nested" / "dir" / "app.db"
    engine = create_engine(f"sqlite+aiosqlite:///{nested.as_posix()}")
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()

    assert nested.parent.is_dir()


async def test_current_revision_is_none_before_migrating(
    engine: AsyncEngine,
) -> None:
    async with create_session_factory(engine)() as session:
        assert await current_revision(session) is None
