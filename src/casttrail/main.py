from contextlib import asynccontextmanager
from datetime import timedelta
from typing import TYPE_CHECKING

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from casttrail import get_version
from casttrail.auth.bootstrap import bootstrap_admin
from casttrail.auth.service import AuthService
from casttrail.config import Settings
from casttrail.db import create_engine, create_session_factory
from casttrail.discovery.schedule import RefreshJob, run_in_background
from casttrail.logging_config import configure_logging
from casttrail.providers.rostercon_client import (
    RosterconFetcher,
    create_client as create_rostercon_client,
)
from casttrail.providers.tmdb import TmdbProvider, create_client
from casttrail.startup import (
    ADMIN_WARNING,
    TIMERS_WARNING,
    ProblemGuard,
    StartupReport,
    prepare_database,
    run_backup_quietly,
)
from casttrail.web import static_files
from casttrail.web.account_routes import router as account_router
from casttrail.web.admin_routes import router as admin_router
from casttrail.web.auth_routes import router as auth_router
from casttrail.web.csrf import verify_same_origin
from casttrail.web.dependencies import LoginRequiredError
from casttrail.web.errors import PageError, render_page_error
from casttrail.web.event_routes import router as event_router
from casttrail.web.match_routes import router as match_router
from casttrail.web.merge_routes import router as merge_router
from casttrail.web.request_log import RequestLogMiddleware
from casttrail.web.review_routes import router as review_router
from casttrail.web.routes import router
from casttrail.web.search_routes import router as search_router
from casttrail.web.watch_routes import router as watch_router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

    import httpx2
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from casttrail.discovery.schedule import Refresher
    from casttrail.providers.base import MetadataProvider

logger = structlog.get_logger(__name__)


async def _redirect_to_login(_request: Request, _exc: Exception) -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


async def _create_first_admin(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Create the administrator named in the environment, if the app has no users."""
    if not (settings.admin_email and settings.admin_password):
        return
    async with session_factory() as session:
        auth = AuthService(
            session, session_ttl=timedelta(hours=settings.session_ttl_hours)
        )
        try:
            created = await bootstrap_admin(
                auth,
                session,
                email=settings.admin_email,
                password=settings.admin_password.get_secret_value(),
                display_name=settings.admin_display_name,
            )
        except OperationalError as error:
            msg = "The database has no tables yet. Run `uv run poe migrate` first."
            raise RuntimeError(msg) from error
    if created:
        logger.info("first_admin_created", email=settings.admin_email)


def _start_scheduler(
    settings: Settings, refresher: Refresher
) -> AsyncIOScheduler | None:
    """Start the timers: the daily refresh (if switched on) and the daily backup."""
    backups = settings.backups_enabled
    if not settings.refresh_enabled and not backups:
        return None
    scheduler = AsyncIOScheduler()
    # Both timers run one at a time, and a start missed by up to an hour (the server
    # was busy or just restarting) still runs once rather than waiting for tomorrow.
    timer = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 3600}
    if settings.refresh_enabled:
        scheduler.add_job(
            run_in_background,
            CronTrigger(hour=settings.refresh_hour, minute=0),
            args=[refresher],
            id="rostercon-refresh",
            **timer,
        )
    if backups:
        scheduler.add_job(
            run_backup_quietly,
            CronTrigger(hour=settings.backup_hour, minute=30),
            args=[settings, "daily"],
            id="daily-backup",
            **timer,
        )
    scheduler.start()
    logger.info(
        "scheduler_started",
        jobs=sorted(job.id for job in scheduler.get_jobs()),
        refresh_hour=settings.refresh_hour if settings.refresh_enabled else None,
        backup_hour=settings.backup_hour if backups else None,
    )
    return scheduler


def _safely_start_scheduler(
    settings: Settings, refresher: Refresher, report: StartupReport
) -> AsyncIOScheduler | None:
    """Start the timers; if that fails, carry on without them and say so."""
    try:
        return _start_scheduler(settings, refresher)
    except Exception:
        logger.exception("scheduler_start_failed")
        report.warnings.append(TIMERS_WARNING)
        return None


async def _safely_create_first_admin(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    report: StartupReport,
) -> None:
    """Create the first administrator; if that fails, carry on and say so."""
    try:
        await _create_first_admin(settings, session_factory)
    except Exception:
        logger.exception("first_admin_failed")
        report.warnings.append(ADMIN_WARNING)


def _settings_problems(error: ValidationError) -> list[str]:
    """Describe invalid settings by name, never by value (they may be secrets)."""
    found: list[str] = []
    for item in error.errors():
        where = ".".join(str(part) for part in item["loc"])
        name = f"CASTTRAIL_{where.upper()}" if where else "Settings"
        found.append(f"{name}: {item['msg']}")
    return found


def _problem_app(problems: list[str]) -> FastAPI:
    """Build an app that only says what is wrong, for when it cannot be configured."""
    app = FastAPI(title="CastTrail", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.startup = StartupReport(problems=problems)
    app.add_middleware(ProblemGuard)
    return app


def _start_providers(
    app: FastAPI,
    settings: Settings,
    metadata_provider: MetadataProvider | None,
    refresher: Refresher | None,
) -> list[httpx2.AsyncClient]:
    """Set up where metadata and convention listings come from.

    Returns the HTTP clients that were opened, for closing at shutdown.
    """
    opened: list[httpx2.AsyncClient] = []
    app.state.metadata = metadata_provider
    if metadata_provider is None and settings.tmdb_read_token is not None:
        tmdb_client = create_client(settings.tmdb_read_token.get_secret_value())
        opened.append(tmdb_client)
        app.state.metadata = TmdbProvider(tmdb_client)
    if refresher is None:
        rostercon_http = create_rostercon_client()
        opened.append(rostercon_http)
        fetcher = RosterconFetcher(rostercon_http)
        app.state.refresher = RefreshJob(app.state.session_factory, fetcher)
    else:
        app.state.refresher = refresher
    return opened


async def _stop(
    app: FastAPI,
    scheduler: AsyncIOScheduler | None,
    clients: list[httpx2.AsyncClient],
    engine: AsyncEngine,
) -> None:
    """Shut down the timers, any refresh still running, and the connections."""
    if scheduler is not None:
        scheduler.shutdown(wait=False)
    task = app.state.refresh_task
    if task is not None and not task.done():
        task.cancel()
    for client in clients:
        await client.aclose()
    await engine.dispose()


def _lifespan(
    settings: Settings,
    metadata_provider: MetadataProvider | None,
    refresher: Refresher | None,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Build the start-up and shut-down steps. Nothing in them is allowed to crash."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "app_starting",
            version=get_version(),
            auto_migrate=settings.auto_migrate,
            backups=settings.backups_enabled,
            refresh=settings.refresh_enabled,
            tmdb_configured=settings.tmdb_read_token is not None,
            log_level=settings.log_level,
        )
        report = await prepare_database(settings)
        app.state.startup = report
        engine = create_engine(settings.database_url)
        app.state.session_factory = create_session_factory(engine)
        clients = _start_providers(app, settings, metadata_provider, refresher)
        app.state.refresh_task = None
        app.state.scheduler = None
        try:
            if not report.problems:
                factory = app.state.session_factory
                await _safely_create_first_admin(settings, factory, report)
                app.state.scheduler = _safely_start_scheduler(
                    settings, app.state.refresher, report
                )
            logger.info(
                "app_ready",
                problems=report.problems,
                warnings=report.warnings,
                serving=not report.problems,
            )
            yield
        finally:
            logger.info("app_stopping")
            await _stop(app, app.state.scheduler, clients, engine)

    return lifespan


def create_app(
    settings: Settings | None = None,
    *,
    metadata_provider: MetadataProvider | None = None,
    refresher: Refresher | None = None,
) -> FastAPI:
    """Build the application.

    Invalid settings do not crash it: the app that comes back only says what is wrong.
    Problems found while starting (the database cannot be updated, say) are held on
    ``app.state.startup`` and shown instead of pages until fixed.

    Args:
        settings: Settings to use. Read from the environment when omitted.
        metadata_provider: Where people, films and series come from. Tests pass a fake
            here; otherwise TMDB is used when its token is set, and search is
            switched off when it is not.
        refresher: What the daily timer and the admin page run to refresh conventions.
            Tests pass a fake; otherwise Rostercon is contacted, but only when a
            refresh actually runs.

    Returns:
        The configured FastAPI application.
    """
    if settings is None:
        try:
            settings = Settings()
        except ValidationError as error:
            configure_logging()
            problems = _settings_problems(error)
            logger.error("settings_invalid", problems=problems)
            return _problem_app(problems)
    configure_logging(settings.log_level, settings.log_format)
    app = FastAPI(
        title="CastTrail",
        version=get_version(),
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=_lifespan(settings, metadata_provider, refresher),
        # Every unsafe request, now and in later routers, must come from our own pages.
        dependencies=[Depends(verify_same_origin)],
    )
    app.state.settings = settings
    app.state.startup = StartupReport()
    app.add_middleware(ProblemGuard)
    # Added last, so it is outermost and also logs what the guard answers.
    app.add_middleware(RequestLogMiddleware)
    app.add_exception_handler(LoginRequiredError, _redirect_to_login)
    app.add_exception_handler(PageError, render_page_error)
    app.mount("/static", static_files(), name="static")
    for included in (
        router,
        auth_router,
        admin_router,
        account_router,
        search_router,
        watch_router,
        event_router,
        match_router,
        merge_router,
        review_router,
    ):
        app.include_router(included)
    return app
