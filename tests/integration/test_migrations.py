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

    assert _revisions(database_url) == ["0015"]


def test_upgrade_is_repeatable(database_url: str) -> None:
    config = _config(database_url)

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    assert _revisions(database_url) == ["0015"]


def test_status_page_shows_the_applied_revision(
    database_url: str,
) -> None:
    command.upgrade(_config(database_url), "head")

    with TestClient(create_app(Settings(database_url=database_url))) as client:
        assert "Database revision 0015" in client.get("/status").text


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
    assert _revisions(database_url) == ["0015"]


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


DISCOVERY_TABLES = {
    "conventions",
    "convention_sources",
    "source_observations",
    "appearances",
    "appearance_sources",
}


def test_upgrading_from_watches_keeps_existing_data(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "0006")
    with closing(_connect(database_url)) as connection:
        connection.executescript(
            f"""
            INSERT INTO users (id, email, display_name, password_hash, is_active,
                               is_admin, created_at)
                VALUES ('u1', 'a@example.com', 'A', 'hash', 1, 1, {NOW});
            INSERT INTO people (id, name, created_at) VALUES ('p1', 'Alice', {NOW});
            INSERT INTO watches (id, user_id, person_id, created_at)
                VALUES ('w1', 'u1', 'p1', {NOW});
            INSERT INTO external_identities (id, provider, external_id, person_id,
                                             created_at)
                VALUES ('e1', 'rostercon', 'alice-en', 'p1', {NOW});
            """
        )

    command.upgrade(config, "head")

    with closing(_connect(database_url)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM watches").fetchone() == (1,)
        assert connection.execute(
            "SELECT external_id FROM external_identities"
        ).fetchall() == [("alice-en",)]
        assert _table_names(connection) >= DISCOVERY_TABLES
    assert _revisions(database_url) == ["0015"]


def test_the_migrated_discovery_schema_enforces_its_constraints(
    database_url: str,
) -> None:
    command.upgrade(_config(database_url), "head")
    with closing(_connect(database_url)) as connection:
        connection.executescript(
            f"""
            INSERT INTO conventions (id, name, created_at, updated_at)
                VALUES ('c1', 'Con', {NOW}, {NOW});
            INSERT INTO convention_sources (id, convention_id, provider, source_key,
                                            source_url, first_seen_at, last_seen_at)
                VALUES ('s1', 'c1', 'rostercon', 'k', 'https://x.test', {NOW}, {NOW});
            INSERT INTO external_identities (id, provider, external_id, created_at)
                VALUES ('e1', 'rostercon', 'g', {NOW});
            INSERT INTO appearances (id, convention_id, external_identity_id,
                                     created_at)
                VALUES ('a1', 'c1', 'e1', {NOW});
            INSERT INTO appearance_sources (id, appearance_id, provider, source_url,
                                            listed, first_seen_at, last_seen_at)
                VALUES ('as1', 'a1', 'rostercon', 'https://x.test', 1, {NOW}, {NOW});
            """
        )

        refused = (
            f"""INSERT INTO conventions (id, name, status, created_at, updated_at)
                VALUES ('c2', 'Bad', 'moved', {NOW}, {NOW})""",
            f"""INSERT INTO convention_sources (id, convention_id, provider, source_key,
                    source_url, first_seen_at, last_seen_at)
                VALUES ('s2', 'c1', 'rostercon', 'k', 'https://x', {NOW}, {NOW})""",
            f"""INSERT INTO appearances (id, convention_id, external_identity_id,
                    created_at) VALUES ('a2', 'c1', 'e1', {NOW})""",
            f"""INSERT INTO appearances (id, convention_id, external_identity_id,
                    created_at) VALUES ('a3', 'c1', 'nobody', {NOW})""",
            f"""INSERT INTO appearance_sources (id, appearance_id, provider, source_url,
                    listed, first_seen_at, last_seen_at)
                VALUES ('as2', 'a1', 'rostercon', 'https://y.test', 1, {NOW}, {NOW})""",
            f"""INSERT INTO appearance_sources (id, appearance_id, provider, source_url,
                    listed, status, first_seen_at, last_seen_at)
                VALUES ('as3', 'a1', 'other', 'https://y', 1, 'gone', {NOW}, {NOW})""",
        )
        for statement in refused:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)

        connection.execute("DELETE FROM conventions WHERE id = 'c1'")
        for table in ("convention_sources", "appearances", "appearance_sources"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (
                0,
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM external_identities"
        ).fetchone() == (1,)


def test_downgrading_removes_only_the_discovery_tables(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, "0006")

    with closing(_connect(database_url)) as connection:
        tables = _table_names(connection)
    assert {"watches", "people", "external_identities"} <= tables
    assert tables.isdisjoint(DISCOVERY_TABLES)
    assert _revisions(database_url) == ["0006"]


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def test_upgrading_from_discovery_tables_keeps_identities_and_adds_a_name(
    database_url: str,
) -> None:
    config = _config(database_url)
    command.upgrade(config, "0007")
    with closing(_connect(database_url)) as connection:
        assert "display_name" not in _columns(connection, "external_identities")
        connection.executescript(
            f"""
            INSERT INTO people (id, name, created_at) VALUES ('p1', 'Alice', {NOW});
            INSERT INTO external_identities (id, provider, external_id, person_id,
                                             created_at)
                VALUES ('e1', 'tmdb', '1', 'p1', {NOW}),
                       ('e2', 'rostercon', 'bob-en', NULL, {NOW});
            """
        )

    command.upgrade(config, "head")

    with closing(_connect(database_url)) as connection:
        assert "display_name" in _columns(connection, "external_identities")
        rows = connection.execute(
            """
            SELECT external_id, person_id, display_name FROM external_identities
            ORDER BY external_id
            """
        ).fetchall()
    assert rows == [("1", "p1", None), ("bob-en", None, None)]
    assert _revisions(database_url) == ["0015"]


def test_the_migrated_name_column_takes_a_name_and_may_stay_empty(
    database_url: str,
) -> None:
    command.upgrade(_config(database_url), "head")
    with closing(_connect(database_url)) as connection:
        connection.executescript(
            f"""
            INSERT INTO external_identities (id, provider, external_id, display_name,
                                             created_at)
                VALUES ('e1', 'rostercon', 'a', 'Paul McGann', {NOW}),
                       ('e2', 'rostercon', 'b', NULL, {NOW});
            """
        )
        names = connection.execute(
            "SELECT display_name FROM external_identities ORDER BY external_id"
        ).fetchall()
    assert names == [("Paul McGann",), (None,)]


def test_downgrading_from_the_name_column_keeps_the_identities(
    database_url: str,
) -> None:
    config = _config(database_url)
    command.upgrade(config, "head")
    with closing(_connect(database_url)) as connection:
        connection.execute(
            f"""
            INSERT INTO external_identities (id, provider, external_id, display_name,
                                             created_at)
                VALUES ('e1', 'rostercon', 'a', 'Paul McGann', {NOW})
            """
        )
        connection.commit()

    command.downgrade(config, "0007")

    with closing(_connect(database_url)) as connection:
        assert "display_name" not in _columns(connection, "external_identities")
        assert connection.execute(
            "SELECT external_id FROM external_identities"
        ).fetchall() == [("a",)]
    assert _revisions(database_url) == ["0007"]


def test_the_migrated_job_runs_table_allows_one_running_row_per_job(
    database_url: str,
) -> None:
    command.upgrade(_config(database_url), "head")
    insert = (
        f"INSERT INTO job_runs (id, job, status, started_at) VALUES (?, ?, ?, {NOW})"
    )
    with closing(_connect(database_url)) as connection:
        connection.execute(insert, ("1", "refresh", "running"))
        # A finished run, and a running run of another job, do not count.
        connection.execute(insert, ("2", "refresh", "failed"))
        connection.execute(insert, ("3", "refresh", "succeeded"))
        connection.execute(insert, ("4", "other", "running"))

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert, ("5", "refresh", "running"))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert, ("6", "refresh", "paused"))

        connection.execute("UPDATE job_runs SET status = 'failed' WHERE id = '1'")
        connection.execute(insert, ("7", "refresh", "running"))


def test_upgrading_from_the_previous_revision_keeps_people_and_adds_lookups(
    database_url: str,
) -> None:
    config = _config(database_url)
    command.upgrade(config, "0013")
    with closing(_connect(database_url)) as connection:
        connection.execute(
            f"INSERT INTO people (id, name, created_at) VALUES ('a', 'Alice', {NOW})"
        )
        connection.commit()
        assert "source_lookups" not in _table_names(connection)

    command.upgrade(config, "head")

    with closing(_connect(database_url)) as connection:
        assert connection.execute("SELECT name FROM people").fetchall() == [("Alice",)]
        assert connection.execute("SELECT COUNT(*) FROM source_lookups").fetchone() == (
            0,
        )
    assert _revisions(database_url) == ["0015"]


def test_the_migrated_source_lookups_table_enforces_its_rules(
    database_url: str,
) -> None:
    command.upgrade(_config(database_url), "head")
    insert = f"""
        INSERT INTO source_lookups (id, provider, person_id, outcome, source_key,
                                    source_url, looked_up_at)
        VALUES (?, ?, ?, ?, ?, ?, {NOW})
    """
    with closing(_connect(database_url)) as connection:
        connection.execute(
            f"INSERT INTO people (id, name, created_at) VALUES ('a', 'Alice', {NOW})"
        )
        connection.execute(insert, ("1", "comiconomicon", "a", "found", "518", "u"))
        # The same person may be looked up on another source, and be not found there.
        connection.execute(insert, ("2", "rostercon", "a", "not_found", None, None))

        refused = (
            # One answer per source and person.
            ("3", "comiconomicon", "a", "not_found", None, None),
            # An outcome we do not know.
            ("4", "other", "a", "maybe", None, None),
            # Found must say what was found, and only found may.
            ("5", "other", "a", "found", None, None),
            ("6", "other", "a", "found", "518", None),
            ("7", "other", "a", "not_found", "518", "u"),
            ("8", "other", "a", "ambiguous", "518", "u"),
            # A lookup belongs to a person who exists.
            ("9", "other", "nobody", "not_found", None, None),
        )
        for row in refused:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(insert, row)

        connection.execute("DELETE FROM people WHERE id = 'a'")
        assert connection.execute("SELECT COUNT(*) FROM source_lookups").fetchone() == (
            0,
        )


def test_downgrading_removes_only_the_source_lookups_table(
    database_url: str,
) -> None:
    config = _config(database_url)
    command.upgrade(config, "head")
    with closing(_connect(database_url)) as connection:
        connection.execute(
            f"INSERT INTO people (id, name, created_at) VALUES ('a', 'Alice', {NOW})"
        )
        connection.commit()

    command.downgrade(config, "0013")

    with closing(_connect(database_url)) as connection:
        assert "source_lookups" not in _table_names(connection)
        assert connection.execute("SELECT name FROM people").fetchall() == [("Alice",)]
    assert _revisions(database_url) == ["0013"]


def test_upgrading_from_source_lookups_keeps_rows_as_not_manual(
    database_url: str,
) -> None:
    config = _config(database_url)
    command.upgrade(config, "0014")
    with closing(_connect(database_url)) as connection:
        connection.execute(
            f"INSERT INTO people (id, name, created_at) VALUES ('a', 'Alice', {NOW})"
        )
        connection.execute(
            f"""
            INSERT INTO source_lookups (id, provider, person_id, outcome, source_key,
                                        source_url, looked_up_at)
            VALUES ('1', 'comiconomicon', 'a', 'found', '518', 'u', {NOW})
            """
        )
        connection.commit()

    command.upgrade(config, "head")

    with closing(_connect(database_url)) as connection:
        assert connection.execute(
            "SELECT source_key, manual FROM source_lookups"
        ).fetchall() == [("518", 0)]
    assert _revisions(database_url) == ["0015"]


def test_only_a_found_lookup_may_be_manual(database_url: str) -> None:
    command.upgrade(_config(database_url), "head")
    insert = f"""
        INSERT INTO source_lookups (id, provider, person_id, outcome, source_key,
                                    source_url, manual, looked_up_at)
        VALUES (?, 'rostercon', 'a', ?, ?, ?, ?, {NOW})
    """
    with closing(_connect(database_url)) as connection:
        connection.execute(
            f"INSERT INTO people (id, name, created_at) VALUES ('a', 'Alice', {NOW})"
        )
        connection.execute(insert, ("1", "found", "tom-wilson-en", "u", 1))
        connection.execute("DELETE FROM source_lookups")

        for outcome in ("not_found", "ambiguous"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(insert, ("2", outcome, None, None, 1))
        # Not manual is allowed for any outcome.
        connection.execute(insert, ("3", "not_found", None, None, 0))


def test_downgrading_from_manual_links_keeps_the_lookups(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "head")
    with closing(_connect(database_url)) as connection:
        connection.execute(
            f"INSERT INTO people (id, name, created_at) VALUES ('a', 'Alice', {NOW})"
        )
        connection.execute(
            f"""
            INSERT INTO source_lookups (id, provider, person_id, outcome, source_key,
                                        source_url, manual, looked_up_at)
            VALUES ('1', 'rostercon', 'a', 'found', 'tom-wilson-en', 'u', 1, {NOW})
            """
        )
        connection.commit()

    command.downgrade(config, "0014")

    with closing(_connect(database_url)) as connection:
        assert "manual" not in _columns(connection, "source_lookups")
        assert connection.execute(
            "SELECT source_key FROM source_lookups"
        ).fetchall() == [("tom-wilson-en",)]
    assert _revisions(database_url) == ["0014"]
