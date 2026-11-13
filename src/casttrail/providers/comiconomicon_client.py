from urllib.parse import urlencode

from casttrail.providers.base import ProviderError
from casttrail.providers.comiconomicon import (
    AUTOCOMPLETE_PATH,
    BASE,
    HOST,
    Suggestion,
    SuggestionList,
    match_by_name,
    parse_autocomplete,
)
from casttrail.providers.polite import PoliteFetcher

# A name longer than this is not a person; it is never sent to the site.
_MAX_TERM = 100


class ComiconomiconUnavailableError(ProviderError):
    """Comiconomicon refused for now (429 or 503), or is still being left alone."""


class ComiconomiconNotFoundError(ProviderError):
    """Comiconomicon has no page at that address."""


class ComiconomiconFetcher(PoliteFetcher):
    """Fetches Comiconomicon pages politely; see ``PoliteFetcher`` for the rules."""

    source = "comiconomicon"
    label = "Comiconomicon"
    host = HOST
    unavailable_error = ComiconomiconUnavailableError
    not_found_error = ComiconomiconNotFoundError


async def search(fetcher: ComiconomiconFetcher, term: str) -> SuggestionList:
    """Ask the site's own search box what it knows under a name.

    Only the request's path is ever logged, so the name searched for stays out of the
    logs.

    Args:
        fetcher: Fetches Comiconomicon pages politely.
        term: The name to search for.

    Returns:
        The usable results. An empty or overlong name sends nothing and finds nothing.

    Raises:
        ProviderError: The site could not be read or its answer was not a list.
    """
    clean = " ".join(term.split())
    if not clean or len(clean) > _MAX_TERM:
        return SuggestionList([], 0)
    page = await fetcher.get(f"{BASE}{AUTOCOMPLETE_PATH}?{urlencode({'term': clean})}")
    return parse_autocomplete(page.text)


async def find_guests(fetcher: ComiconomiconFetcher, name: str) -> list[Suggestion]:
    """Look a person up on Comiconomicon by searching for their name.

    Args:
        fetcher: Fetches Comiconomicon pages politely.
        name: The person's name as TMDB gives it.

    Returns:
        The guests whose whole name is the same: none if the site does not know the
        name, one if it is clear, several if it is ambiguous and the caller must not
        pick.

    Raises:
        ProviderError: The site could not be read.
    """
    found = await search(fetcher, name)
    return match_by_name(found.items, "guest", name)
