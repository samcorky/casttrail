import re

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from casttrail import get_version
from casttrail.db import current_revision
from casttrail.providers.tmdb import IMAGE_BASE_URL
from casttrail.web import WEB_DIR
from casttrail.web.dependencies import CurrentUser, SessionDep, WatchServiceDep

router = APIRouter()
templates = Jinja2Templates(directory=WEB_DIR / "templates")

# TMDB image paths look like "/abc123.jpg". Anything else is refused, because the path
# comes from outside and ends up in a URL.
_IMAGE_PATH = re.compile(r"/[A-Za-z0-9_-]+\.(?:jpg|png|webp)")
_IMAGE_SIZES = frozenset({"w92", "w185", "w342"})


def tmdb_image(path: str | None, size: str = "w185") -> str | None:
    """Build the address of a TMDB poster or profile photo.

    Args:
        path: The image path TMDB gave, such as ``/abc123.jpg``.
        size: One of TMDB's fixed widths.

    Returns:
        The full URL, or ``None`` if there is no usable image.
    """
    if path is None or size not in _IMAGE_SIZES or not _IMAGE_PATH.fullmatch(path):
        return None
    return f"{IMAGE_BASE_URL}/{size}{path}"


templates.env.globals["tmdb_image"] = tmdb_image


@router.get("/health")
async def health(session: SessionDep, response: Response) -> dict[str, str]:
    """Liveness probe. Discloses nothing about the configuration.

    Args:
        session: A database session.
        response: The response, so the status code can be changed.

    Returns:
        ``ok``, or ``unavailable`` with a 503 when the database cannot be reached.
    """
    try:
        await session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        response.status_code = 503
        return {"status": "unavailable"}
    return {"status": "ok"}


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> RedirectResponse:
    """Send browsers that ask for the default favicon path to the real icon.

    Returns:
        A permanent redirect to the 32 pixel PNG.
    """
    return RedirectResponse("/static/img/favicon-32.png", status_code=308)


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request, user: CurrentUser, service: WatchServiceDep
) -> HTMLResponse:
    """Render the signed-in user's own dashboard.

    Args:
        request: The current request.
        user: The signed-in user.
        service: The watch service.

    Returns:
        The rendered page.
    """
    watches = await service.list_watches(user.id)
    people = sum(1 for w in watches if w.person is not None)
    context = {"user": user, "people": people, "productions": len(watches) - people}
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/status", response_class=HTMLResponse)
async def status(request: Request, session: SessionDep) -> HTMLResponse:
    """Render the status page. It works before login and before migration.

    Args:
        request: The current request.
        session: A database session.

    Returns:
        The rendered page.
    """
    context = {
        "version": get_version(),
        "revision": await current_revision(session),
    }
    return templates.TemplateResponse(request, "status.html", context)
