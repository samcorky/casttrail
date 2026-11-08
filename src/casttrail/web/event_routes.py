import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response

from casttrail.discovery.browse import event_detail, upcoming_events
from casttrail.web.dependencies import CurrentUser, SessionDep
from casttrail.web.errors import PageError
from casttrail.web.routes import templates

router = APIRouter()


@router.get("/events", response_class=HTMLResponse)
async def events(
    request: Request,
    user: CurrentUser,
    session: SessionDep,
    scope: Literal["uk", "all"] = "uk",
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
) -> Response:
    """List upcoming conventions, UK ones unless asked for all.

    Args:
        request: The current request.
        user: The signed-in user.
        session: The request's database session.
        scope: ``uk`` or ``all``.
        page: The page, from 1.

    Returns:
        The page.
    """
    uk_only = scope == "uk"
    rows, more = await upcoming_events(
        session, today=datetime.now(UTC).date(), uk_only=uk_only, page=page
    )
    context = {
        "user": user,
        "rows": rows,
        "uk_only": uk_only,
        "page": page,
        "more": more,
    }
    return templates.TemplateResponse(request, "events.html", context)


@router.get("/events/{convention_id}", response_class=HTMLResponse)
async def event(
    request: Request, convention_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Response:
    """Show one convention and its guests.

    Args:
        request: The current request.
        convention_id: The convention.
        user: The signed-in user.
        session: The request's database session.

    Returns:
        The page.

    Raises:
        PageError: A 404 if there is no such convention.
    """
    detail = await event_detail(session, convention_id)
    if detail is None:
        raise PageError("That event was not found.", status_code=404)
    return templates.TemplateResponse(
        request, "event.html", {"user": user, "detail": detail}
    )
