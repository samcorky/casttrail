import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import OperationalError

from casttrail import get_version
from casttrail.auth.bootstrap import bootstrap_admin
from casttrail.auth.service import AuthService
from casttrail.config import Settings
from casttrail.db import create_engine, create_session_factory
from casttrail.discovery.schedule import RefreshJob, run_in_background
from casttrail.providers.rostercon_client import (
    PeopleIndex,
    RosterconFetcher,
    create_client as create_rostercon_client,
)
from casttrail.providers.tmdb import TmdbProvider, create_client
from casttrail.web import static_files
from casttrail.web.account_routes import router as account_router
from casttrail.web.admin_routes import router as admin_router
from casttrail.web.auth_routes import router as auth_router
from casttrail.web.csrf import verify_same_origin
from casttrail.web.dependencies import LoginRequiredError
from casttrail.web.errors import PageError, render_page_error
from casttrail.web.event_routes import router as event_router
from casttrail.web.match_routes import router as match_router
from casttrail.web.review_routes import router as review_router
from casttrail.web.routes import router
from casttrail.web.search_routes import router as search_router
from casttrail.web.watch_routes import router as watch_router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from casttrail.discovery.schedule import Refresher
    from casttrail.providers.base import MetadataProvider

logger = logging.getLogger(__name__)


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
        logger.info("Created the first administrator %s", settings.admin_email)


def _start_scheduler(
    settings: Settings, refresher: Refresher
) -> AsyncIOScheduler | None:
    """Start the daily refresh timer, if it is switched on."""
    if not settings.refresh_enabled:
        return None
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_in_background,
        CronTrigger(hour=settings.refresh_hour, minute=0),
        args=[refresher],
        id="rostercon-refresh",
        # One at a time, and a start missed by up to an hour (the server was busy or
        # just restarting) still runs once rather than waiting for tomorrow.
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    return scheduler


def create_app(
    settings: Settings | None = None,
    *,
    metadata_provider: MetadataProvider | None = None,
    refresher: Refresher | None = None,
) -> FastAPI:
    """Build the application.

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
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        app.state.session_factory = create_session_factory(engine)
        tmdb_client = None
        app.state.metadata = metadata_provider
        if metadata_provider is None and settings.tmdb_read_token is not None:
            tmdb_client = create_client(settings.tmdb_read_token.get_secret_value())
            app.state.metadata = TmdbProvider(tmdb_client)
        rostercon_http = None
        if refresher is None:
            rostercon_http = create_rostercon_client()
            fetcher = RosterconFetcher(rostercon_http)
            app.state.refresher = RefreshJob(
                app.state.session_factory, fetcher, PeopleIndex(fetcher)
            )
        else:
            app.state.refresher = refresher
        app.state.refresh_task = None
        scheduler = _start_scheduler(settings, app.state.refresher)
        app.state.scheduler = scheduler
        try:
            await _create_first_admin(settings, app.state.session_factory)
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)
            task = app.state.refresh_task
            if task is not None and not task.done():
                task.cancel()
            if rostercon_http is not None:
                await rostercon_http.aclose()
            if tmdb_client is not None:
                await tmdb_client.aclose()
            await engine.dispose()

    app = FastAPI(
        title="CastTrail",
        version=get_version(),
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
        # Every unsafe request, now and in later routers, must come from our own pages.
        dependencies=[Depends(verify_same_origin)],
    )
    app.state.settings = settings
    app.add_exception_handler(LoginRequiredError, _redirect_to_login)
    app.add_exception_handler(PageError, render_page_error)
    app.mount("/static", static_files(), name="static")
    app.include_router(router)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(account_router)
    app.include_router(search_router)
    app.include_router(watch_router)
    app.include_router(event_router)
    app.include_router(match_router)
    app.include_router(review_router)
    return app
