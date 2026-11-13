import uuid
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from casttrail.discovery.links import InvalidLinkError, UnknownPersonError
from casttrail.web.dependencies import CurrentAdmin, RosterconLinksDep
from casttrail.web.errors import PageError
from casttrail.web.routes import templates

router = APIRouter(prefix="/admin/links")

_EXAMPLE = "https://www.rostercon.com/en/people/tom-wilson-en"
_NOTICES = {
    "linked": "Linked. The next refresh will use this page.",
    "invalid": (
        f"Not a Rostercon person page. Paste the person's own page, like {_EXAMPLE}."
    ),
    "unlinked": "Removed. The next refresh will look for this person by name again.",
    "none": "There was no link set by hand to remove.",
}


def _done(notice: str) -> RedirectResponse:
    return RedirectResponse(
        f"/admin/links?{urlencode({'notice': notice})}", status_code=303
    )


@router.get("", response_class=HTMLResponse)
async def links_page(
    request: Request,
    admin: CurrentAdmin,
    links: RosterconLinksDep,
    notice: str | None = None,
) -> Response:
    """Show every watched person and their Rostercon page.

    Args:
        request: The current request.
        admin: The signed-in administrator.
        links: The Rostercon links service.
        notice: Which message to show after an action.

    Returns:
        The page.
    """
    people = await links.watched()
    context = {
        "user": admin,
        "people": people,
        "attention": sum(p.state in ("not_found", "ambiguous") for p in people),
        "notice": _NOTICES.get(notice or ""),
        "invalid": notice == "invalid",
    }
    return templates.TemplateResponse(request, "admin_links.html", context)


@router.post("/set")
async def set_link(
    admin: CurrentAdmin,
    links: RosterconLinksDep,
    person_id: Annotated[uuid.UUID, Form()],
    url: Annotated[str, Form(max_length=300)],
) -> RedirectResponse:
    """Point a person at their Rostercon page.

    Args:
        admin: The signed-in administrator.
        links: The Rostercon links service.
        person_id: The person.
        url: The pasted address of their Rostercon page.

    Returns:
        A redirect back to the page.

    Raises:
        PageError: A 404 if the person does not exist.
    """
    try:
        await links.link(person_id, url)
    except InvalidLinkError:
        return _done("invalid")
    except UnknownPersonError as error:
        raise PageError("That person was not found.", status_code=404) from error
    return _done("linked")


@router.post("/remove")
async def remove_link(
    admin: CurrentAdmin,
    links: RosterconLinksDep,
    person_id: Annotated[uuid.UUID, Form()],
) -> RedirectResponse:
    """Remove a link set by hand.

    Args:
        admin: The signed-in administrator.
        links: The Rostercon links service.
        person_id: The person.

    Returns:
        A redirect back to the page.
    """
    return _done("unlinked" if await links.unlink(person_id) else "none")
