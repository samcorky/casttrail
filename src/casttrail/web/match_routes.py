from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from casttrail.matching.queries import relevant_appearances
from casttrail.web.dependencies import CurrentUser, SessionDep
from casttrail.web.routes import templates

router = APIRouter()


@router.get("/appearances", response_class=HTMLResponse)
async def appearances(
    request: Request,
    user: CurrentUser,
    session: SessionDep,
    scope: Literal["uk", "all"] = "uk",
) -> Response:
    """Show where the people the signed-in user watches will be appearing.

    UK conventions only unless asked for all, as on the events page.

    Args:
        request: The current request.
        user: The signed-in user. Only their own matches are shown.
        session: The request's database session.
        scope: ``uk`` or ``all``.

    Returns:
        The page.
    """
    uk_only = scope == "uk"
    rows = await relevant_appearances(
        session, user.id, today=datetime.now(UTC).date(), uk_only=uk_only
    )
    context = {"user": user, "rows": rows, "uk_only": uk_only}
    return templates.TemplateResponse(request, "appearances.html", context)
