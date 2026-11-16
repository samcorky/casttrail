import uuid

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from casttrail.notifications.queries import mark_all_read, mark_read, user_alerts
from casttrail.web.dependencies import CurrentUser, SessionDep
from casttrail.web.errors import PageError
from casttrail.web.routes import templates

logger = structlog.get_logger(__name__)
router = APIRouter()


@router.get("/alerts", response_class=HTMLResponse)
async def alerts(request: Request, user: CurrentUser, session: SessionDep) -> Response:
    """Show the signed-in user's alerts, newest first.

    Args:
        request: The current request.
        user: The signed-in user. Only their own alerts are shown.
        session: The request's database session.

    Returns:
        The page.
    """
    rows = await user_alerts(session, user.id)
    context = {"user": user, "rows": rows, "any_unread": any(r.unread for r in rows)}
    return templates.TemplateResponse(request, "alerts.html", context)


@router.post("/alerts/{alert_id}/read")
async def read_one(
    alert_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> RedirectResponse:
    """Mark one alert read.

    Args:
        alert_id: The alert.
        user: The signed-in user. Only their own alerts can be marked.
        session: The request's database session.

    Returns:
        A redirect back to the alerts.

    Raises:
        PageError: A 404 if this user has no such alert.
    """
    if not await mark_read(session, user.id, alert_id):
        raise PageError("That alert was not found.", status_code=404)
    return RedirectResponse("/alerts", status_code=303)


@router.post("/alerts/read")
async def read_all(user: CurrentUser, session: SessionDep) -> RedirectResponse:
    """Mark every alert of the signed-in user read.

    Args:
        user: The signed-in user. Only their own alerts are marked.
        session: The request's database session.

    Returns:
        A redirect back to the alerts.
    """
    marked = await mark_all_read(session, user.id)
    logger.info("alerts_marked_read", count=marked)
    return RedirectResponse("/alerts", status_code=303)
