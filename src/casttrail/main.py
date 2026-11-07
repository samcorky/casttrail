from fastapi import FastAPI

from casttrail import get_version
from casttrail.config import Settings
from casttrail.web import static_files
from casttrail.web.routes import router


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Args:
        settings: Settings to use. Read from the environment when omitted.

    Returns:
        The configured FastAPI application.
    """
    settings = settings or Settings()
    docs = "/docs" if settings.docs_enabled else None
    app = FastAPI(
        title="CastTrail",
        version=get_version(),
        docs_url=docs,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.mount("/static", static_files(), name="static")
    app.include_router(router)
    return app
