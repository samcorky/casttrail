from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from casttrail.matching.queries import relevant_appearances
from casttrail.web.dependencies import CurrentUser, SessionDep
from casttrail.web.routes import templates

router = APIRouter()


@router.get("/appearances", response_class=HTMLResponse)
async def appearances(
    request: Request, user: CurrentUser, session: SessionDep
) -> Response:
    """Show where the people the signed-in user watches will be appearing.

    Args:
        request: The current request.
        user: The signed-in user. Only their own matches are shown.
        session: The request's database session.

    Returns:
        The page.
    """
    rows = await relevant_appearances(session, user.id, today=datetime.now(UTC).date())
    return templates.TemplateResponse(
        request, "appearances.html", {"user": user, "rows": rows}
    )
