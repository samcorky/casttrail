"""Getting the app into a usable state at startup, without ever crashing.

A problem here must not stop the server. It is recorded as a *problem* (the app cannot
serve until someone looks) or a *warning* (it works, but something needs a look), logged
in full, and described in plain words that hold no paths or secrets.
"""

import asyncio
import time
from dataclasses import dataclass, field
from html import escape
from typing import TYPE_CHECKING

import structlog
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

import casttrail.maintenance as maintenance
from casttrail.config import Settings  # noqa: TC001  (the scheduler reads annotations)

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from fastapi import Request, Response
    from starlette.middleware.base import RequestResponseEndpoint


logger = structlog.get_logger(__name__)

TIMERS_WARNING = "The daily timers could not be started. Nothing will run by itself."
ADMIN_WARNING = "The first administrator could not be created. See the server log."
CHECK_PROBLEM = "The database could not be checked. It may be from a newer version."
BACKUP_FIRST_PROBLEM = (
    "The database was not updated, because a copy could not be taken."
)
MIGRATE_PROBLEM = "The database could not be updated. A copy from before is in backups."
FOLDER_PROBLEM = "The data folder could not be created."
BACKUP_WARNING = "A backup could not be taken. See the server log."


@dataclass
class StartupReport:
    """What went wrong while starting, if anything."""

    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


async def run_backup(
    settings: Settings, reason: str, *, now: datetime | None = None
) -> Path | None:
    """Take a backup in a worker thread, then drop old daily copies.

    Args:
        settings: The application settings.
        reason: Why the copy is being taken (see ``maintenance.REASONS``).
        now: The time to record, replaceable in tests.

    Returns:
        The new file, or ``None`` if backups are off or there is no database file.
    """
    file = maintenance.database_file(settings.database_url)
    folder = maintenance.backup_folder(settings.database_url, settings.backup_dir)
    if not settings.backups_enabled or file is None or folder is None:
        return None
    started = time.perf_counter()
    made = await asyncio.to_thread(maintenance.backup, file, folder, reason, now=now)
    removed = await asyncio.to_thread(
        maintenance.prune_backups, folder, settings.backup_keep
    )
    logger.info(
        "backup_taken",
        reason=reason,
        file=made.name,
        size_bytes=made.stat().st_size,
        pruned=len(removed),
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return made


async def run_backup_quietly(settings: Settings, reason: str) -> None:
    """Take a backup where nobody is waiting, logging a failure instead of raising.

    Args:
        settings: The application settings.
        reason: Why the copy is being taken.
    """
    try:
        await run_backup(settings, reason)
    except Exception:
        logger.exception("backup_failed", reason=reason)


async def prepare_database(settings: Settings) -> StartupReport:
    """Make the database ready: folder, backup, migration, and a catch-up backup.

    Never raises. If the data folder cannot be made, the database cannot be updated, or
    a copy cannot be taken first, that is a problem and nothing is changed further. A
    failed catch-up backup is only a warning.

    Args:
        settings: The application settings.

    Returns:
        What went wrong, if anything.
    """
    report = StartupReport()
    file = maintenance.database_file(settings.database_url)
    if file is None:
        return report
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("data_folder_failed", folder=file.parent.name)
        report.problems.append(FOLDER_PROBLEM)
        return report
    folder = maintenance.backup_folder(settings.database_url, settings.backup_dir)
    if settings.auto_migrate and not await _migrate(settings, file, folder, report):
        return report
    await _catch_up_backup(settings, file, folder, report)
    return report


async def _migrate(
    settings: Settings, file: Path, folder: Path | None, report: StartupReport
) -> bool:
    """Bring the database up to date. Returns whether it is safe to carry on."""
    try:
        pending = await asyncio.to_thread(maintenance.migration_pending, file)
    except Exception:
        logger.exception("migration_check_failed")
        report.problems.append(CHECK_PROBLEM)
        return False
    before = await asyncio.to_thread(maintenance.current_revision, file)
    if not pending:
        logger.info("database_up_to_date", revision=before)
        return True
    has_data = before is not None
    logger.info(
        "migration_pending", from_revision=before, to_revision=maintenance.head()
    )
    if has_data and settings.backups_enabled and folder is not None:
        try:
            await asyncio.to_thread(
                maintenance.backup, file, folder, "before-migration"
            )
        except Exception:
            logger.exception("migration_backup_failed", from_revision=before)
            report.problems.append(BACKUP_FIRST_PROBLEM)
            return False
    started = time.perf_counter()
    try:
        await asyncio.to_thread(maintenance.migrate, settings.database_url)
    except Exception:
        logger.exception("migration_failed", from_revision=before)
        report.problems.append(MIGRATE_PROBLEM)
        return False
    logger.info(
        "migration_done",
        from_revision=before,
        to_revision=await asyncio.to_thread(maintenance.current_revision, file),
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return True


async def _catch_up_backup(
    settings: Settings, file: Path, folder: Path | None, report: StartupReport
) -> None:
    """Take a backup now if the newest is over a day old (the PC may sleep at night)."""
    if not settings.backups_enabled or folder is None:
        return
    try:
        has_data = await asyncio.to_thread(maintenance.current_revision, file)
        due = await asyncio.to_thread(maintenance.backup_is_due, folder)
        if has_data is not None and due:
            await run_backup(settings, "startup")
    except Exception:
        logger.exception("startup_backup_failed")
        report.warnings.append(BACKUP_WARNING)


_PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CastTrail needs attention</title>
<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto">
<h1>CastTrail needs attention</h1>
<p>The app started, but cannot serve pages yet:</p>
<ul>{items}</ul>
<p>The details are in the server log. Fix the cause and restart the app.</p>
</body>
</html>
"""


def maintenance_page(problems: list[str]) -> HTMLResponse:
    """Render the page shown while the app cannot serve.

    It stands alone (no templates, no database, no static files) so it still works
    when everything else is broken.

    Args:
        problems: Plain descriptions, free of paths and secrets.

    Returns:
        A 503 page, told not to be cached.
    """
    items = "".join(f"<li>{escape(problem)}</li>" for problem in problems)
    return HTMLResponse(
        _PAGE.format(items=items),
        status_code=503,
        headers={"Cache-Control": "no-store"},
    )


class ProblemGuard(BaseHTTPMiddleware):
    """While the app has a startup problem, answer every page with the problem page.

    Static files are still served. ``/health`` says only that the app is degraded.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Let the request through, unless the app is in trouble.

        Args:
            request: The incoming request.
            call_next: The rest of the app.

        Returns:
            The app's answer, or a 503 while there is a problem.
        """
        report: StartupReport = request.app.state.startup
        if not report.problems or request.url.path.startswith("/static/"):
            return await call_next(request)
        if request.url.path == "/health":
            return JSONResponse(
                {"status": "degraded"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return maintenance_page(report.problems)
