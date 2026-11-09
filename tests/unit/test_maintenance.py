import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from casttrail import maintenance
from casttrail.maintenance import (
    backup,
    backup_folder,
    backup_is_due,
    current_revision,
    database_file,
    list_backups,
    migrate,
    migration_pending,
    prune_backups,
)

NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)


def _make_db(path: Path, rows: int = 3) -> Path:
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE t (n INTEGER)")
        db.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
        db.commit()
    return path


def _rows(path: Path) -> list[int]:
    with closing(sqlite3.connect(path)) as db:
        return [r[0] for r in db.execute("SELECT n FROM t ORDER BY n")]


# --- where things are -------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite+aiosqlite:///data/casttrail.db", Path("data/casttrail.db")),
        ("sqlite+aiosqlite:///:memory:", None),
        ("sqlite+aiosqlite://", None),
    ],
)
def test_the_database_file_comes_from_the_url(url: str, expected: Path | None) -> None:
    assert database_file(url) == expected


def test_backups_default_to_a_folder_beside_the_database() -> None:
    url = "sqlite+aiosqlite:///data/casttrail.db"

    assert backup_folder(url, None) == Path("data/backups")
    assert backup_folder(url, "") == Path("data/backups")
    assert backup_folder(url, "elsewhere") == Path("elsewhere")


def test_a_database_with_no_file_has_nowhere_to_back_up_to() -> None:
    assert backup_folder("sqlite+aiosqlite:///:memory:", None) is None
    assert backup_folder("sqlite+aiosqlite:///:memory:", "x") == Path("x")


# --- migrations -------------------------------------------------------------------


def test_a_missing_or_empty_or_broken_file_has_no_revision(tmp_path: Path) -> None:
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a database")

    assert current_revision(tmp_path / "none.db") is None
    assert current_revision(_make_db(tmp_path / "plain.db")) is None
    assert current_revision(junk) is None


def test_a_new_database_is_pending_and_a_migrated_one_is_not(tmp_path: Path) -> None:
    file = tmp_path / "app.db"
    url = f"sqlite+aiosqlite:///{file.as_posix()}"
    assert migration_pending(file) is True

    migrate(url)

    assert migration_pending(file) is False
    assert current_revision(file) == maintenance._head()


def test_migrating_twice_is_harmless(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}"

    migrate(url)
    migrate(url)

    assert migration_pending(tmp_path / "app.db") is False


def test_a_database_behind_the_code_is_pending(tmp_path: Path) -> None:
    file = tmp_path / "app.db"
    with closing(sqlite3.connect(file)) as db:
        db.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        db.execute("INSERT INTO alembic_version VALUES ('0001')")
        db.commit()

    assert current_revision(file) == "0001"
    assert migration_pending(file) is True


def test_a_url_containing_a_percent_sign_survives_the_config(tmp_path: Path) -> None:
    folder = tmp_path / "100% data"
    folder.mkdir()
    file = folder / "app.db"

    migrate(f"sqlite+aiosqlite:///{file.as_posix()}")

    assert migration_pending(file) is False


# --- taking a backup --------------------------------------------------------------


def test_a_backup_is_a_complete_independent_copy(tmp_path: Path) -> None:
    source = _make_db(tmp_path / "app.db", rows=5)

    made = backup(source, tmp_path / "backups", "manual", now=NOW)

    assert made.name == "casttrail-20261109-120000-manual.db"
    assert _rows(made) == [0, 1, 2, 3, 4]
    with closing(sqlite3.connect(source)) as db:
        db.execute("INSERT INTO t VALUES (99)")
        db.commit()
    assert 99 not in _rows(made)
    assert not list((tmp_path / "backups").glob("*.partial"))


def test_a_backup_works_while_the_database_is_open_for_writing(tmp_path: Path) -> None:
    source = _make_db(tmp_path / "app.db")
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO t VALUES (50)")
        writer.commit()

        made = backup(source, tmp_path / "b", "daily", now=NOW)

    assert 50 in _rows(made)


def test_a_backup_never_changes_the_original(tmp_path: Path) -> None:
    source = _make_db(tmp_path / "app.db")
    before = source.read_bytes()

    backup(source, tmp_path / "b", "daily", now=NOW)

    assert source.read_bytes() == before


@pytest.mark.parametrize("reason", ["", "Daily", "weekly", "../x", "daily.db"])
def test_an_unknown_reason_is_refused_and_writes_nothing(
    tmp_path: Path, reason: str
) -> None:
    source = _make_db(tmp_path / "app.db")

    with pytest.raises(ValueError, match="Unknown backup reason"):
        backup(source, tmp_path / "b", reason, now=NOW)

    assert not (tmp_path / "b").exists()


def test_backing_up_a_missing_database_raises_and_makes_no_folder(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        backup(tmp_path / "none.db", tmp_path / "b", "daily", now=NOW)

    assert not (tmp_path / "b").exists()


def test_a_failed_backup_leaves_no_file_that_looks_like_one(tmp_path: Path) -> None:
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all, just bytes" * 10)

    with pytest.raises(sqlite3.Error):
        backup(junk, tmp_path / "b", "daily", now=NOW)

    assert list((tmp_path / "b").iterdir()) == []


def test_a_cleanup_failure_does_not_hide_why_the_backup_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all, just bytes" * 10)

    def cannot_delete(self: Path, missing_ok: bool = False) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(Path, "unlink", cannot_delete)

    with pytest.raises(sqlite3.Error):
        backup(junk, tmp_path / "b", "daily", now=NOW)


# --- listing and pruning ----------------------------------------------------------


def _touch(folder: Path, when: datetime, reason: str) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    name = f"casttrail-{when:%Y%m%d-%H%M%S}-{reason}.db"
    (folder / name).write_bytes(b"x" * 10)
    return name


def test_backups_are_listed_newest_first_and_other_files_are_ignored(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "b"
    old = _touch(folder, NOW - timedelta(days=2), "daily")
    new = _touch(folder, NOW, "manual")
    (folder / "notes.txt").write_text("hi", encoding="utf-8")
    (folder / "casttrail-20261109-120000-evil.db.partial").write_bytes(b"x")
    (folder / "casttrail-2026-bad-daily.db").write_bytes(b"x")
    (folder / "casttrail-20261109-120000-Daily.db").write_bytes(b"x")
    (folder / "casttrail-20261109-130000-daily.db").mkdir()

    listed = list_backups(folder)

    assert [b.name for b in listed] == [new, old]
    assert [b.reason for b in listed] == ["manual", "daily"]
    assert listed[0].taken_at == NOW
    assert listed[0].size == 10


def test_a_missing_folder_has_no_backups(tmp_path: Path) -> None:
    assert list_backups(tmp_path / "nope") == []


def test_pruning_keeps_the_newest_daily_copies_and_every_other_kind(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "b"
    dailies = [_touch(folder, NOW - timedelta(days=d), "daily") for d in range(5)]
    others = [
        _touch(folder, NOW - timedelta(days=30), reason)
        for reason in ("manual", "startup", "before-migration")
    ]

    removed = prune_backups(folder, keep=2)

    assert sorted(removed) == sorted(dailies[2:])
    left = {b.name for b in list_backups(folder)}
    assert left == {*dailies[:2], *others}


def test_pruning_fewer_than_the_limit_removes_nothing(tmp_path: Path) -> None:
    folder = tmp_path / "b"
    _touch(folder, NOW, "daily")

    assert prune_backups(folder, keep=5) == []
    assert prune_backups(tmp_path / "none", keep=5) == []


# --- when a backup is due ---------------------------------------------------------


def test_a_backup_is_due_when_there_is_none(tmp_path: Path) -> None:
    assert backup_is_due(tmp_path / "none", now=NOW) is True


def test_a_backup_is_due_only_when_the_newest_is_over_a_day_old(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "b"
    _touch(folder, NOW - timedelta(days=1), "daily")

    assert backup_is_due(folder, now=NOW) is False
    assert backup_is_due(folder, now=NOW + timedelta(seconds=1)) is True


def test_a_manual_copy_counts_towards_not_being_due(tmp_path: Path) -> None:
    folder = tmp_path / "b"
    _touch(folder, NOW - timedelta(days=9), "daily")
    _touch(folder, NOW - timedelta(hours=1), "manual")

    assert backup_is_due(folder, now=NOW) is False


def test_the_migrations_folder_defaults_to_the_one_beside_the_source() -> None:
    assert maintenance.MIGRATIONS_DIR.name == "migrations"
    assert (maintenance.MIGRATIONS_DIR / "env.py").is_file()


def test_an_installed_copy_can_say_where_its_migrations_are(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import importlib

    monkeypatch.setenv("CASTTRAIL_MIGRATIONS_DIR", str(tmp_path))
    try:
        reloaded = importlib.reload(maintenance)
        assert tmp_path == reloaded.MIGRATIONS_DIR
    finally:
        monkeypatch.delenv("CASTTRAIL_MIGRATIONS_DIR")
        importlib.reload(maintenance)
