import uuid
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from casttrail.matching.review import (
    find_guests,
    linked_guests,
    people_choices,
    suggestions,
)
from casttrail.web.dependencies import (
    CurrentAdmin,
    DecisionServiceDep,
    MatchServiceDep,
    SessionDep,
)
from casttrail.web.errors import PageError
from casttrail.web.routes import templates

router = APIRouter(prefix="/admin/review")

_NOTICES = {
    "confirmed": "Confirmed.",
    "rejected": "Rejected. That person will not be suggested for this guest again.",
    "linked": "Linked.",
    "unlinked": "Unlinked.",
    "none": "Nothing to confirm.",
}


def _done(notice: str, q: str = "") -> RedirectResponse:
    query = {"notice": notice, **({"q": q} if q else {})}
    return RedirectResponse(f"/admin/review?{urlencode(query)}", status_code=303)


@router.get("", response_class=HTMLResponse)
async def review(
    request: Request,
    admin: CurrentAdmin,
    session: SessionDep,
    q: str = "",
    notice: str | None = None,
) -> Response:
    """Show guests waiting to be confirmed, linked guests, and a guest search.

    Args:
        request: The current request.
        admin: The signed-in administrator.
        session: The request's database session.
        q: Part of a guest's name to look for.
        notice: Which message to show after an action.

    Returns:
        The page.
    """
    q = q[:100]
    found = await find_guests(session, q)
    context = {
        "user": admin,
        "suggestions": await suggestions(session),
        "linked": await linked_guests(session),
        "found": found,
        "q": q,
        "people": await people_choices(session) if found else [],
        "notice": _NOTICES.get(notice or ""),
    }
    return templates.TemplateResponse(request, "admin_review.html", context)


@router.post("/confirm")
async def confirm(
    admin: CurrentAdmin,
    decisions: DecisionServiceDep,
    matches: MatchServiceDep,
    identity_id: Annotated[uuid.UUID, Form()],
    person_id: Annotated[uuid.UUID, Form()],
    q: Annotated[str, Form(max_length=100)] = "",
) -> RedirectResponse:
    """Say a guest is a person (also used to link a guest to any person).

    Args:
        admin: The signed-in administrator.
        decisions: The decision service.
        matches: The match service.
        identity_id: The guest.
        person_id: The person.
        q: The search to go back to.

    Returns:
        A redirect back to the review page.

    Raises:
        PageError: A 404 if the guest or person does not exist.
    """
    if not await decisions.confirm(identity_id, person_id, admin.id):
        raise PageError("That guest or person was not found.", status_code=404)
    await matches.rematch()
    return _done("confirmed", q)


@router.post("/reject")
async def reject(
    admin: CurrentAdmin,
    decisions: DecisionServiceDep,
    matches: MatchServiceDep,
    identity_id: Annotated[uuid.UUID, Form()],
    person_id: Annotated[uuid.UUID, Form()],
    q: Annotated[str, Form(max_length=100)] = "",
) -> RedirectResponse:
    """Say a guest is not a person.

    Args:
        admin: The signed-in administrator.
        decisions: The decision service.
        matches: The match service.
        identity_id: The guest.
        person_id: The person they are not.
        q: The search to go back to.

    Returns:
        A redirect back to the review page.

    Raises:
        PageError: A 404 if the guest or person does not exist.
    """
    if not await decisions.reject(identity_id, person_id, admin.id):
        raise PageError("That guest or person was not found.", status_code=404)
    await matches.rematch()
    return _done("rejected", q)


@router.post("/unlink")
async def unlink(
    admin: CurrentAdmin,
    decisions: DecisionServiceDep,
    matches: MatchServiceDep,
    identity_id: Annotated[uuid.UUID, Form()],
    q: Annotated[str, Form(max_length=100)] = "",
) -> RedirectResponse:
    """Remove a guest's link to a person.

    Args:
        admin: The signed-in administrator.
        decisions: The decision service.
        matches: The match service.
        identity_id: The guest.
        q: The search to go back to.

    Returns:
        A redirect back to the review page.

    Raises:
        PageError: A 404 if the guest does not exist.
    """
    del admin  # Only checked: any administrator may unlink.
    if not await decisions.unlink(identity_id):
        raise PageError("That guest was not found.", status_code=404)
    await matches.rematch()
    return _done("unlinked", q)


@router.post("/confirm-all")
async def confirm_all(
    admin: CurrentAdmin,
    session: SessionDep,
    decisions: DecisionServiceDep,
    matches: MatchServiceDep,
) -> RedirectResponse:
    """Confirm every guest whose exact name matches exactly one watched person.

    A guest with more than one suggestion is left for a person to decide.

    Args:
        admin: The signed-in administrator.
        session: The request's database session.
        decisions: The decision service.
        matches: The match service.

    Returns:
        A redirect back to the review page.
    """
    pending = await suggestions(session)
    counts: dict[uuid.UUID, int] = {}
    for item in pending:
        counts[item.identity_id] = counts.get(item.identity_id, 0) + 1
    single = [item for item in pending if counts[item.identity_id] == 1]
    for item in single:
        await decisions.confirm(item.identity_id, item.person_id, admin.id)
    if not single:
        return _done("none")
    await matches.rematch()
    return _done("confirmed")
