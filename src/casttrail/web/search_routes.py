import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from casttrail.providers.base import ProviderError
from casttrail.web.dependencies import CurrentUser, MetadataDep
from casttrail.web.routes import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search")

MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 100


@router.get("", response_class=HTMLResponse, response_model=None)
async def search(
    request: Request, user: CurrentUser, metadata: MetadataDep, q: str = ""
) -> Response:
    """Search for people, films and series.

    The whole page is returned normally, so search works without JavaScript. HTMX asks
    for the same URL and gets only the results.

    Args:
        request: The current request.
        user: The signed-in user.
        metadata: Where people, films and series come from, if it is set up.
        q: What was typed.

    Returns:
        The page, or just the results fragment for an HTMX request.
    """
    query = q.strip()
    context: dict[str, object] = {
        "user": user,
        "q": query,
        "message": None,
        "people": [],
        "films": [],
        "series": [],
    }
    if metadata is None:
        context["message"] = (
            "Search is not set up. An administrator must configure TMDB."
        )
    elif not query:
        pass
    elif len(query) < MIN_QUERY_LENGTH:
        context["message"] = f"Type at least {MIN_QUERY_LENGTH} characters."
    elif len(query) > MAX_QUERY_LENGTH:
        context["message"] = f"Search for at most {MAX_QUERY_LENGTH} characters."
    else:
        try:
            people, productions = await asyncio.gather(
                metadata.search_people(query), metadata.search_productions(query)
            )
        except ProviderError as error:
            # The message holds no credentials or URLs, so it is safe to log.
            logger.warning("Search failed: %s", error)
            context["message"] = "Search is unavailable right now. Try again shortly."
        else:
            context["people"] = people
            context["films"] = [p for p in productions if p.media_type == "movie"]
            context["series"] = [p for p in productions if p.media_type == "tv"]
            if not (people or productions):
                context["message"] = f"Nothing found for “{query}”."
    template = (
        "search_results.html"
        if request.headers.get("HX-Request") == "true"
        else "search.html"
    )
    return templates.TemplateResponse(
        request, template, context, headers={"Vary": "HX-Request"}
    )
