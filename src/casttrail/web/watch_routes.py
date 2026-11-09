import uuid
from typing import Annotated, Literal
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from casttrail.providers.base import MediaType, ProviderError
from casttrail.web.dependencies import (
    CurrentUser,
    MatchServiceDep,
    MetadataDep,
    WatchServiceDep,
)
from casttrail.web.errors import PageError
from casttrail.web.routes import templates

logger = structlog.get_logger(__name__)

router = APIRouter()

# Where to go after a watch or unwatch. Only these two, so a form can never be used to
# send someone to an address of its choosing.
ReturnTo = Literal["search", "watchlist"]


def _back(return_to: ReturnTo, q: str) -> RedirectResponse:
    if return_to == "search" and q:
        return RedirectResponse(f"/search?{urlencode({'q': q})}", status_code=303)
    if return_to == "search":
        return RedirectResponse("/search", status_code=303)
    return RedirectResponse("/watchlist", status_code=303)


@router.get("/watchlist", response_class=HTMLResponse)
async def watchlist(
    request: Request, user: CurrentUser, service: WatchServiceDep
) -> Response:
    """Show what the signed-in user is watching.

    Args:
        request: The current request.
        user: The signed-in user.
        service: The watch service.

    Returns:
        The page.
    """
    watches = await service.list_watches(user.id)
    context = {
        "user": user,
        "people": [w for w in watches if w.person is not None],
        "films": [
            w for w in watches if w.production and w.production.media_type == "movie"
        ],
        "series": [
            w for w in watches if w.production and w.production.media_type == "tv"
        ],
    }
    return templates.TemplateResponse(request, "watchlist.html", context)


@router.post("/watches/people")
async def watch_person(
    user: CurrentUser,
    metadata: MetadataDep,
    service: WatchServiceDep,
    matches: MatchServiceDep,
    tmdb_id: Annotated[int, Form(gt=0)],
    return_to: Annotated[ReturnTo, Form()] = "search",
    q: Annotated[str, Form(max_length=100)] = "",
) -> RedirectResponse:
    """Start watching a person.

    The details come from TMDB, never from the form, so nothing the browser sends
    ends up in the catalogue.

    Args:
        user: The signed-in user.
        metadata: Where people come from.
        service: The watch service.
        matches: The match service, so the new watch shows up at once.
        tmdb_id: TMDB's id for the person.
        return_to: Which page to go back to.
        q: The search to go back to.

    Returns:
        A redirect back.

    Raises:
        PageError: If TMDB is not set up, cannot be reached, or has no such person.
    """
    if metadata is None:
        raise PageError(
            "Search is not set up, so nothing can be added.", status_code=503
        )
    try:
        person = await metadata.person(tmdb_id)
    except ProviderError as error:
        logger.warning("tmdb_person_lookup_failed", tmdb_id=tmdb_id, error=str(error))
        raise PageError(
            "TMDB is unavailable right now. Try again shortly.", status_code=502
        ) from error
    if person is None:
        raise PageError("TMDB has no such person.", status_code=404)
    await service.watch_person(user.id, person)
    logger.info("watch_added", kind="person", tmdb_id=tmdb_id)
    await matches.rematch()
    return _back(return_to, q)


@router.post("/watches/productions")
async def watch_production(
    user: CurrentUser,
    metadata: MetadataDep,
    service: WatchServiceDep,
    media_type: Annotated[MediaType, Form()],
    tmdb_id: Annotated[int, Form(gt=0)],
    return_to: Annotated[ReturnTo, Form()] = "search",
    q: Annotated[str, Form(max_length=100)] = "",
) -> RedirectResponse:
    """Start watching a film or a series.

    Args:
        user: The signed-in user.
        metadata: Where films and series come from.
        service: The watch service.
        media_type: Whether it is a film or a series.
        tmdb_id: TMDB's id for it.
        return_to: Which page to go back to.
        q: The search to go back to.

    Returns:
        A redirect back.

    Raises:
        PageError: If TMDB is not set up, cannot be reached, or has no such title.
    """
    if metadata is None:
        raise PageError(
            "Search is not set up, so nothing can be added.", status_code=503
        )
    try:
        production = await metadata.production(media_type, tmdb_id)
    except ProviderError as error:
        logger.warning(
            "tmdb_production_lookup_failed",
            media_type=media_type,
            tmdb_id=tmdb_id,
            error=str(error),
        )
        raise PageError(
            "TMDB is unavailable right now. Try again shortly.", status_code=502
        ) from error
    if production is None:
        raise PageError("TMDB has no such film or series.", status_code=404)
    await service.watch_production(user.id, production)
    logger.info("watch_added", kind=media_type, tmdb_id=tmdb_id)
    return _back(return_to, q)


@router.post("/watches/{watch_id}/delete")
async def unwatch(
    watch_id: uuid.UUID,
    user: CurrentUser,
    service: WatchServiceDep,
    matches: MatchServiceDep,
    return_to: Annotated[ReturnTo, Form()] = "watchlist",
    q: Annotated[str, Form(max_length=100)] = "",
) -> RedirectResponse:
    """Stop watching something.

    Args:
        watch_id: The watch to remove.
        user: The signed-in user. Only their own watches can be removed.
        service: The watch service.
        matches: The match service, so removed watches stop matching at once.
        return_to: Which page to go back to.
        q: The search to go back to.

    Returns:
        A redirect back.

    Raises:
        PageError: A 404 if there is no such watch for this user.
    """
    if not await service.unwatch(user.id, watch_id):
        raise PageError("That watch was not found.", status_code=404)
    logger.info("watch_removed", watch_id=str(watch_id))
    await matches.rematch()
    return _back(return_to, q)
