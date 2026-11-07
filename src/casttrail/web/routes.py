from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from casttrail import get_version
from casttrail.db import current_revision
from casttrail.web import WEB_DIR

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

router = APIRouter()
templates = Jinja2Templates(directory=WEB_DIR / "templates")


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Provide a database session for the duration of a request.

    Args:
        request: The current request.

    Yields:
        An open session.
    """
    async with request.app.state.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


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
async def home(request: Request, session: SessionDep) -> HTMLResponse:
    """Render the home and status page.

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
    return templates.TemplateResponse(request, "home.html", context)
