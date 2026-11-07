from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from casttrail import get_version
from casttrail.web import WEB_DIR

router = APIRouter()
templates = Jinja2Templates(directory=WEB_DIR / "templates")


def version_dependency() -> str:
    """Provide the application version to handlers.

    Returns:
        The installed package version.
    """
    return get_version()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Deliberately discloses nothing about the configuration.

    Returns:
        A static ``ok`` status.
    """
    return {"status": "ok"}


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request, version: Annotated[str, Depends(version_dependency)]
) -> HTMLResponse:
    """Render the home and status page.

    Args:
        request: The current request.
        version: The application version.

    Returns:
        The rendered page.
    """
    return templates.TemplateResponse(request, "home.html", {"version": version})
