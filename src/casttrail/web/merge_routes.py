import uuid
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from casttrail.web.dependencies import CurrentAdmin, MatchServiceDep, MergeServiceDep
from casttrail.web.errors import PageError
from casttrail.web.routes import templates

router = APIRouter(prefix="/admin/merges")

_NOTICES = {
    "merged": "The two events are now one.",
    "rejected": "Marked as different events. They will not be suggested again.",
    "reconsidered": "That pair will be suggested again.",
    "same-source": (
        "Not merged: one source lists both, so they are two events of that source."
    ),
}


def _done(notice: str) -> RedirectResponse:
    return RedirectResponse(f"/admin/merges?notice={notice}", status_code=303)


@router.get("", response_class=HTMLResponse)
async def merges(
    request: Request,
    admin: CurrentAdmin,
    service: MergeServiceDep,
    notice: str | None = None,
) -> Response:
    """Show events that might be the same, and ones marked as different.

    Args:
        request: The current request.
        admin: The signed-in administrator.
        service: The merge service.
        notice: Which message to show after an action.

    Returns:
        The page.
    """
    context = {
        "user": admin,
        "suggested": await service.pairs("suggested"),
        "rejected": await service.pairs("rejected"),
        "notice": _NOTICES.get(notice or ""),
    }
    return templates.TemplateResponse(request, "admin_merges.html", context)


@router.post("/{pair_id}/merge")
async def merge(
    admin: CurrentAdmin,
    service: MergeServiceDep,
    matches: MatchServiceDep,
    pair_id: uuid.UUID,
    keep: Annotated[uuid.UUID, Form()],
) -> RedirectResponse:
    """Join the two events of a suggested pair, keeping the one chosen.

    Args:
        admin: The signed-in administrator.
        service: The merge service.
        matches: The match service, so matches follow the joined guests.
        pair_id: The suggested pair.
        keep: Which of the pair's two events to keep.

    Returns:
        A redirect back to the page.

    Raises:
        PageError: A 404 if there is no such suggested pair.
    """
    del admin  # Only checked: any administrator may merge.
    outcome = await service.merge(pair_id, keep)
    if outcome == "missing":
        raise PageError("That pair was not found.", status_code=404)
    if outcome == "same-source":
        return _done("same-source")
    await matches.rematch()
    return _done("merged")


@router.post("/{pair_id}/reject")
async def reject(
    admin: CurrentAdmin, service: MergeServiceDep, pair_id: uuid.UUID
) -> RedirectResponse:
    """Say a suggested pair are different events.

    Args:
        admin: The signed-in administrator.
        service: The merge service.
        pair_id: The suggested pair.

    Returns:
        A redirect back to the page.

    Raises:
        PageError: A 404 if there is no such suggested pair.
    """
    del admin
    if not await service.reject(pair_id):
        raise PageError("That pair was not found.", status_code=404)
    return _done("rejected")


@router.post("/{pair_id}/reconsider")
async def reconsider(
    admin: CurrentAdmin, service: MergeServiceDep, pair_id: uuid.UUID
) -> RedirectResponse:
    """Take back a rejection, so the pair is suggested again.

    Args:
        admin: The signed-in administrator.
        service: The merge service.
        pair_id: The rejected pair.

    Returns:
        A redirect back to the page.

    Raises:
        PageError: A 404 if there is no such rejected pair.
    """
    del admin
    if not await service.reconsider(pair_id):
        raise PageError("That pair was not found.", status_code=404)
    return _done("reconsidered")
