from dataclasses import dataclass

from casttrail.providers.base import ProviderError
from casttrail.providers.polite import PoliteFetcher
from casttrail.providers.rostercon import HOST, PERSON_PATH, PersonPage, slugify_name

# Most people share a name with nobody; this stops a page that answers every address
# from keeping us probing.
_MAX_SAME_NAME = 5


class RosterconUnavailableError(ProviderError):
    """Rostercon refused for now (429 or 503), or is still being left alone."""


class RosterconNotFoundError(ProviderError):
    """Rostercon has no page at that address."""


class RosterconFetcher(PoliteFetcher):
    """Fetches Rostercon pages politely; see ``PoliteFetcher`` for the rules."""

    source = "rostercon"
    label = "Rostercon"
    host = HOST
    unavailable_error = RosterconUnavailableError
    not_found_error = RosterconNotFoundError


@dataclass(frozen=True)
class FoundPerson:
    """A Rostercon person page that exists, with the page as it was fetched."""

    page: PersonPage
    text: str


async def find_people(fetcher: RosterconFetcher, name: str) -> list[FoundPerson]:
    """Look a name up on Rostercon by asking for the page it would have.

    Rostercon's person pages are ``/en/people/<name-as-words>``, sometimes ending in
    ``-en``, and a second person with the same name gets ``-2``, a third ``-3``. Each
    candidate is asked for directly, so nothing needs listing first. For each number the
    first form that exists is used. Counting stops at the first number with no page.

    Args:
        fetcher: Fetches Rostercon pages politely.
        name: The person's name as TMDB gives it.

    Returns:
        The pages that exist: none if Rostercon does not know the name, one if it is
        clear, several if the name is ambiguous and the caller must not pick.

    Raises:
        ProviderError: Rostercon could not be read. A page that is simply missing is
            not an error.
    """
    base = slugify_name(name)
    if not base:
        return []
    found: list[FoundPerson] = []
    for number in range(1, _MAX_SAME_NAME + 1):
        stem = base if number == 1 else f"{base}-{number}"
        hit = await _first_page(fetcher, (f"{stem}-en", stem))
        if hit is not None:
            found.append(hit)
        elif number > 1:
            break
    return found


async def _first_page(
    fetcher: RosterconFetcher, slugs: tuple[str, ...]
) -> FoundPerson | None:
    for slug in slugs:
        url = f"https://{HOST}{PERSON_PATH}{slug}"
        try:
            page = await fetcher.get(url)
        except RosterconNotFoundError:
            continue
        return FoundPerson(PersonPage(slug=slug, url=url), page.text)
    return None
