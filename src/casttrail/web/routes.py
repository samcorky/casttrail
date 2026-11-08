from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from casttrail import get_version
from casttrail.db import current_revision
from casttrail.web import WEB_DIR
from casttrail.web.dependencies import CurrentUser, SessionDep

router = APIRouter()
templates = Jinja2Templates(directory=WEB_DIR / "templates")


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


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: CurrentUser) -> HTMLResponse:
    """Render the signed-in user's own dashboard.

    Args:
        request: The current request.
        user: The signed-in user.

    Returns:
        The rendered page.
    """
    return templates.TemplateResponse(request, "dashboard.html", {"user": user})


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
