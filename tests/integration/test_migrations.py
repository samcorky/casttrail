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

    assert _revisions(database_url) == ["0006"]


def test_upgrade_is_repeatable(database_url: str) -> None:
    config = _config(database_url)

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    assert _revisions(database_url) == ["0006"]


def test_status_page_shows_the_applied_revision(
    database_url: str,
) -> None:
    command.upgrade(_config(database_url), "head")

    with TestClient(create_app(Settings(database_url=database_url))) as client:
        assert "Database revision 0006" in client.get("/status").text


def _connect(database_url: str) -> sqlite3.Connection:
    path = database_url.removeprefix("sqlite+aiosqlite:///")
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row[0] for row in rows}


NEW_TABLES = {"people", "productions", "credits", "external_identities"}
NOW = "'2026-11-08 12:00:00'"


def test_upgrading_from_the_previous_revision_keeps_existing_data(
    database_url: str,
) -> None:
    config = _config(database_url)
    command.upgrade(config, "0004")
    with closing(_connect(database_url)) as connection:
        connection.execute(
            f"""
            INSERT INTO users (id, email, display_name, password_hash, is_active,
                               is_admin, created_at)
            VALUES ('11111111-1111-1111-1111-111111111111', 'a@example.com', 'A',
                    'hash', 1, 1, {NOW})
            """
        )
        connection.commit()

    command.upgrade(config, "head")

    with closing(_connect(database_url)) as connection:
        assert connection.execute("SELECT email FROM users").fetchall() == [
            ("a@example.com",)
        ]
        assert _table_names(connection) >= NEW_TABLES
    assert _revisions(database_url) == ["0006"]


def test_the_migrated_schema_enforces_its_constraints(database_url: str) -> None:
    command.upgrade(_config(database_url), "head")
    with closing(_connect(database_url)) as connection:
        connection.executescript(
            f"""
            INSERT INTO productions (id, media_type, tmdb_id, title, created_at)
                VALUES ('p1', 'tv', 1, 'Show', {NOW});
            INSERT INTO people (id, name, created_at) VALUES ('a', 'Alice', {NOW});
            INSERT INTO credits (id, person_id, production_id) VALUES ('c1', 'a', 'p1');
            INSERT INTO external_identities (id, provider, external_id, person_id,
                                             created_at)
                VALUES ('e1', 'tmdb', '1', 'a', {NOW});
            """
        )

        # The same rules the model tests check, here against the migrated tables.
        refused = (
            f"""INSERT INTO productions (id, media_type, tmdb_id, title, created_at)
                VALUES ('p2', 'tv', 1, 'Duplicate', {NOW})""",
            f"""INSERT INTO productions (id, media_type, tmdb_id, title, created_at)
                VALUES ('p3', 'person', 9, 'Not a film', {NOW})""",
            "INSERT INTO credits (id, person_id, production_id) VALUES ('c2','a','p1')",
            "INSERT INTO credits (id, person_id, production_id) VALUES ('c3','x','p1')",
            f"""INSERT INTO external_identities (id, provider, external_id, created_at)
                VALUES ('e2', 'tmdb', '1', {NOW})""",
        )
        for statement in refused:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)

        connection.execute("DELETE FROM people WHERE id = 'a'")
        assert connection.execute("SELECT COUNT(*) FROM credits").fetchone() == (0,)
        assert connection.execute(
            "SELECT person_id FROM external_identities"
        ).fetchall() == [(None,)]


def test_downgrading_removes_only_the_new_tables(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, "0004")

    with closing(_connect(database_url)) as connection:
        tables = _table_names(connection)
    assert {"users", "sessions", "known_devices"} <= tables
    assert tables.isdisjoint(NEW_TABLES)
