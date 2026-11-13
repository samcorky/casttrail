import uuid
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from casttrail.discovery.links import (
    SOURCES,
    InvalidLinkError,
    LinkSource,
    SourceLink,
    SourceLinks,
    UnknownPersonError,
    source_by_provider,
)
from casttrail.web.dependencies import CurrentAdmin, SessionDep
from casttrail.web.errors import PageError
from casttrail.web.routes import templates

router = APIRouter(prefix="/admin/links")

_NOTICES = {
    "linked": "Linked. The next refresh will use this page.",
    "unlinked": "Removed. The next refresh will look for this person by name again.",
    "none": "There was no link set by hand to remove.",
}


@dataclass(frozen=True)
class PersonRow:
    """A watched person, with where they stand on each source."""

    person_id: uuid.UUID
    name: str
    cells: list[tuple[LinkSource, SourceLink]]


def _done(notice: str, source: LinkSource | None = None) -> RedirectResponse:
    query = {"notice": notice, **({"source": source.provider} if source else {})}
    return RedirectResponse(f"/admin/links?{urlencode(query)}", status_code=303)


def _source(provider: str) -> LinkSource:
    source = source_by_provider(provider)
    if source is None:
        raise PageError("That source was not found.", status_code=404)
    return source


def _message(notice: str | None, provider: str | None) -> str | None:
    if notice == "invalid":
        source = source_by_provider(provider or "")
        if source is None:
            return None
        return f"Not a {source.label} page. Paste its address, like {source.example}."
    return _NOTICES.get(notice or "")


@router.get("", response_class=HTMLResponse)
async def links_page(
    request: Request,
    admin: CurrentAdmin,
    session: SessionDep,
    notice: str | None = None,
    source: str | None = None,
) -> Response:
    """Show every watched person and their page on each source.

    Args:
        request: The current request.
        admin: The signed-in administrator.
        session: The request's database session.
        notice: Which message to show after an action.
        source: Which source the message is about.

    Returns:
        The page.
    """
    people: dict[uuid.UUID, PersonRow] = {}
    attention = []
    for each in SOURCES:
        rows = await SourceLinks(session, each).watched()
        for row in rows:
            people.setdefault(
                row.person_id, PersonRow(row.person_id, row.name, [])
            ).cells.append((each, row))
        needing = sum(row.state in ("not_found", "ambiguous") for row in rows)
        if needing:
            attention.append((each.label, needing))
    message = _message(notice, source)
    context = {
        "user": admin,
        "sources": SOURCES,
        "people": list(people.values()),
        "attention": attention,
        "notice": message,
        "invalid": notice == "invalid" and message is not None,
    }
    return templates.TemplateResponse(request, "admin_links.html", context)


@router.post("/set")
async def set_link(
    admin: CurrentAdmin,
    session: SessionDep,
    provider: Annotated[str, Form(max_length=50)],
    person_id: Annotated[uuid.UUID, Form()],
    url: Annotated[str, Form(max_length=300)],
) -> RedirectResponse:
    """Point a person at their page on a source.

    Args:
        admin: The signed-in administrator.
        session: The request's database session.
        provider: The source, such as ``rostercon``.
        person_id: The person.
        url: The pasted address of their page.

    Returns:
        A redirect back to the page.

    Raises:
        PageError: A 404 if the source or person does not exist.
    """
    source = _source(provider)
    try:
        await SourceLinks(session, source).link(person_id, url)
    except InvalidLinkError:
        return _done("invalid", source)
    except UnknownPersonError as error:
        raise PageError("That person was not found.", status_code=404) from error
    return _done("linked")


@router.post("/remove")
async def remove_link(
    admin: CurrentAdmin,
    session: SessionDep,
    provider: Annotated[str, Form(max_length=50)],
    person_id: Annotated[uuid.UUID, Form()],
) -> RedirectResponse:
    """Remove a link set by hand.

    Args:
        admin: The signed-in administrator.
        session: The request's database session.
        provider: The source, such as ``rostercon``.
        person_id: The person.

    Returns:
        A redirect back to the page.

    Raises:
        PageError: A 404 if the source does not exist.
    """
    source = _source(provider)
    removed = await SourceLinks(session, source).unlink(person_id)
    return _done("unlinked" if removed else "none")
