from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from casttrail import get_version
from casttrail.config import Settings
from casttrail.db import create_engine, create_session_factory
from casttrail.web import static_files
from casttrail.web.routes import router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Args:
        settings: Settings to use. Read from the environment when omitted.

    Returns:
        The configured FastAPI application.
    """
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        app.state.session_factory = create_session_factory(engine)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="CastTrail",
        version=get_version(),
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
    )
    app.mount("/static", static_files(), name="static")
    app.include_router(router)
    return app
