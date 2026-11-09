"""Keeping the database file safe and up to date: migrations and backups.

Everything here works on the SQLite file directly, with the standard library, so it
can run before the application's own engine exists and in a worker thread.
"""

import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
BACKUP_SUFFIX = ".db"
_NAME = re.compile(r"casttrail-(\d{8})-(\d{6})-([a-z-]+)\.db")
# Reasons a backup was taken. Only ``daily`` copies are pruned by age.
REASONS = ("daily", "manual", "startup", "before-migration")


def database_file(url: str) -> Path | None:
    """Return the file a SQLite URL points at.

    Args:
        url: A database URL.

    Returns:
        The path, or ``None`` for an in-memory or non-file database.
    """
    database = make_url(url).database
    if not database or database == ":memory:":
        return None
    return Path(database)


def backup_folder(url: str, configured: str | None) -> Path | None:
    """Work out where backups go.

    Args:
        url: The database URL.
        configured: The folder from settings, if one was given.

    Returns:
        The folder, or ``None`` if there is no database file to back up.
    """
    if configured:
        return Path(configured)
    file = database_file(url)
    return None if file is None else file.parent / "backups"


def _head() -> str | None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return ScriptDirectory.from_config(config).get_current_head()


def current_revision(file: Path) -> str | None:
    """Read which migration a database file is at, without altering it.

    Args:
        file: The SQLite file.

    Returns:
        The revision, or ``None`` if the file is missing or has no migration record.
    """
    if not file.exists():
        return None
    try:
        with closing(
            sqlite3.connect(f"file:{file.as_posix()}?mode=ro", uri=True)
        ) as db:
            row = db.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else str(row[0])


def migration_pending(file: Path) -> bool:
    """Say whether the database is behind the code.

    Args:
        file: The SQLite file.

    Returns:
        ``True`` if a migration would change anything. A new, empty database counts.
    """
    return current_revision(file) != _head()


def migrate(url: str) -> None:
    """Bring the database up to date.

    Runs Alembic synchronously, so call it from a worker thread inside the app.

    Args:
        url: The database URL.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")


def _stamp(moment: datetime, reason: str) -> str:
    return f"casttrail-{moment:%Y%m%d-%H%M%S}-{reason}{BACKUP_SUFFIX}"


def backup(
    file: Path, folder: Path, reason: str, *, now: datetime | None = None
) -> Path:
    """Copy the database safely while the app may still be using it.

    Uses SQLite's own online backup, so the copy is consistent even with writers.
    The copy is written under a temporary name and renamed when complete, so a crash
    never leaves a half-written file that looks like a backup.

    Args:
        file: The database file.
        folder: Where to put the copy. Created if needed.
        reason: One of ``REASONS``; it is part of the file name.
        now: The time to record, replaceable in tests.

    Returns:
        The path of the finished copy.

    Raises:
        ValueError: If the reason is not a known one.
        FileNotFoundError: If there is no database file.
    """
    if reason not in REASONS:
        msg = f"Unknown backup reason: {reason}"
        raise ValueError(msg)
    if not file.exists():
        raise FileNotFoundError(file.name)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / _stamp(now or datetime.now(UTC), reason)
    partial = target.with_suffix(".partial")
    try:
        with (
            closing(
                sqlite3.connect(f"file:{file.as_posix()}?mode=ro", uri=True)
            ) as src,
            closing(sqlite3.connect(partial)) as dst,
        ):
            src.backup(dst)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


@dataclass(frozen=True)
class BackupInfo:
    """One backup copy found in the folder."""

    name: str
    taken_at: datetime
    reason: str
    size: int


def list_backups(folder: Path) -> list[BackupInfo]:
    """List the backups in a folder, newest first. Other files are ignored.

    Args:
        folder: The backup folder.

    Returns:
        The backups.
    """
    if not folder.is_dir():
        return []
    found: list[BackupInfo] = []
    for item in folder.iterdir():
        match = _NAME.fullmatch(item.name)
        if match is None or not item.is_file():
            continue
        taken = datetime.strptime(
            f"{match.group(1)}{match.group(2)}", "%Y%m%d%H%M%S"
        ).replace(tzinfo=UTC)
        found.append(BackupInfo(item.name, taken, match.group(3), item.stat().st_size))
    return sorted(found, key=lambda b: (b.taken_at, b.name), reverse=True)


def prune_backups(folder: Path, keep: int) -> list[str]:
    """Delete the oldest daily backups beyond the newest ``keep``.

    Manual, startup and before-migration copies are never deleted here: someone asked
    for them, or they are the way back from a bad migration.

    Args:
        folder: The backup folder.
        keep: How many daily copies to keep.

    Returns:
        The names that were deleted.
    """
    daily = [b for b in list_backups(folder) if b.reason == "daily"]
    removed: list[str] = []
    for old in daily[keep:]:
        (folder / old.name).unlink(missing_ok=True)
        removed.append(old.name)
    return removed


def backup_is_due(folder: Path, *, now: datetime | None = None) -> bool:
    """Say whether the newest backup is more than a day old, or there is none.

    Args:
        folder: The backup folder.
        now: The time to compare with, replaceable in tests.

    Returns:
        ``True`` if a backup should be taken.
    """
    copies = list_backups(folder)
    return not copies or (now or datetime.now(UTC)) - copies[0].taken_at > timedelta(
        days=1
    )
