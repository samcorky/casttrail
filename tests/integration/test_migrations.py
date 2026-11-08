import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from starlette.testclient import TestClient

from casttrail.config import Settings
from casttrail.main import create_app

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"


def _config(database_url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _revisions(database_url: str) -> list[str]:
    path = database_url.removeprefix("sqlite+aiosqlite:///")
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    return [row[0] for row in rows]


# These tests are synchronous: Alembic's env.py starts its own event loop.


def test_upgrade_on_an_empty_database_reaches_head(database_url: str) -> None:
    command.upgrade(_config(database_url), "head")

    assert _revisions(database_url) == ["0004"]


def test_upgrade_is_repeatable(database_url: str) -> None:
    config = _config(database_url)

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    assert _revisions(database_url) == ["0004"]


def test_status_page_shows_the_applied_revision(
    database_url: str,
) -> None:
    command.upgrade(_config(database_url), "head")

    with TestClient(create_app(Settings(database_url=database_url))) as client:
        assert "Database revision 0004" in client.get("/status").text
