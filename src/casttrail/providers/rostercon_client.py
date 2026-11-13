import asyncio
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx2
import structlog

from casttrail import get_version
from casttrail.providers.base import ProviderError
from casttrail.providers.rostercon import HOST, PERSON_PATH, PersonPage, slugify_name

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = structlog.get_logger(__name__)

# Rostercon starts answering 503 after about 25 requests in 30 minutes, so requests are
# spaced well apart and a refusal is respected for a long time, never retried at once.
MIN_INTERVAL = 10.0
MAX_COOLDOWN = 3600.0
MAX_BODY_BYTES = 5_000_000

# Most people share a name with nobody; this stops a page that answers every address
# from keeping us probing.
_MAX_SAME_NAME = 5


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


class RosterconUnavailableError(ProviderError):
    """Rostercon refused for now (429 or 503), or is still being left alone."""


class RosterconNotFoundError(ProviderError):
    """Rostercon has no page at that address."""


@dataclass(frozen=True)
class FetchedPage:
    """A page as Rostercon sent it."""

    text: str
    etag: str | None


def create_client(
    *, transport: httpx2.AsyncBaseTransport | None = None
) -> httpx2.AsyncClient:
    """Build the HTTP client Rostercon requests go through.

    Redirects are not followed, so a request can never be sent somewhere that was not
    checked first.

    Args:
        transport: Replaces the network. Tests pass a mock here.

    Returns:
        A client the caller must close.
    """
    return httpx2.AsyncClient(
        headers={"User-Agent": f"CastTrail/{get_version()} (personal, self-hosted)"},
        timeout=httpx2.Timeout(20.0),
        follow_redirects=False,
        transport=transport,
    )


def _retry_after(response: httpx2.Response) -> float | None:
    """Read a ``Retry-After`` given in seconds. A date, or nonsense, is ignored."""
    value = response.headers.get("retry-after", "").strip()
    if value.isascii() and value.isdigit():
        return float(value)
    return None


async def _read_limited(response: httpx2.Response) -> bytes:
    """Read a body, giving up as soon as it passes the limit.

    The size is checked as the body arrives, on the decoded bytes, so neither a large
    ``Content-Length`` nor a small download that expands (a compression bomb) is read
    in full before being refused.
    """
    declared = response.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise ProviderError("Rostercon sent a page larger than expected.")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise ProviderError("Rostercon sent a page larger than expected.")
    return bytes(body)


class RosterconFetcher:
    """Fetches Rostercon pages one at a time, slowly, and backs off when told to.

    Requests are never sent faster than ``min_interval``, even from several tasks at
    once.
    After a 429 or 503 nothing is sent until a cooldown has passed: the longer of what
    Rostercon asked for and a delay that doubles with each refusal in a row. Calls made
    during the cooldown fail at once without touching the network.
    """

    def __init__(
        self,
        client: httpx2.AsyncClient,
        *,
        min_interval: float = MIN_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Create the fetcher.

        Args:
            client: The HTTP client, from ``create_client``.
            min_interval: The least time between two requests, in seconds.
            clock: A monotonic clock, replaceable in tests.
            sleep: Waits for a number of seconds, replaceable in tests.
        """
        self._client = client
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_request: float | None = None
        self._blocked_until: float | None = None
        self._refusals = 0

    async def get(self, url: str) -> FetchedPage:
        """Fetch one page.

        Args:
            url: An ``https://www.rostercon.com/...`` address.

        Returns:
            The page text and its ``ETag`` if it sent one.

        Raises:
            RosterconUnavailableError: Rostercon refused, or a cooldown is running.
            RosterconNotFoundError: There is no page at that address.
            ProviderError: The address is not Rostercon's, the request failed, or the
                answer was not a page we can use.
        """
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != HOST or parts.username:
            raise ProviderError("Not a Rostercon address.")
        async with self._lock:
            self._check_cooldown()
            await self._wait_turn()
            return await self._fetch(url)

    def _check_cooldown(self) -> None:
        if self._blocked_until is None:
            return
        remaining = self._blocked_until - self._clock()
        if remaining > 0:
            logger.info("rostercon_cooling_down", remaining_s=round(remaining))
            raise RosterconUnavailableError(
                "Rostercon is being left alone for now.",
                retry_after=timedelta(seconds=remaining),
            )

    async def _wait_turn(self) -> None:
        if self._last_request is not None:
            wait = self._last_request + self._min_interval - self._clock()
            if wait > 0:
                logger.debug("rostercon_spacing", wait_s=round(wait, 1))
                await self._sleep(wait)

    async def _fetch(self, url: str) -> FetchedPage:
        self._last_request = self._clock()
        started = time.perf_counter()
        path = urlsplit(url).path
        status: int | None = None
        try:
            async with self._client.stream("GET", url) as response:
                status = response.status_code
                self._check_status(response)
                body = await _read_limited(response)
                encoding = response.encoding or "utf-8"
                etag = response.headers.get("etag")
        except httpx2.HTTPError as error:
            logger.warning(
                "rostercon_unreachable",
                path=path,
                error=type(error).__name__,
                duration_ms=_ms(started),
            )
            raise ProviderError("Rostercon could not be reached.") from error
        except RosterconNotFoundError as error:
            # Expected whenever a name is not on Rostercon, so not worth a warning.
            logger.info("rostercon_not_found", path=path, duration_ms=_ms(started))
            raise error
        except ProviderError as error:
            logger.warning(
                "rostercon_refused",
                path=path,
                status=status,
                error=str(error),
                duration_ms=_ms(started),
            )
            raise
        logger.info(
            "rostercon_request",
            path=path,
            status=status,
            bytes=len(body),
            duration_ms=_ms(started),
        )
        return FetchedPage(text=body.decode(encoding, errors="replace"), etag=etag)

    def _check_status(self, response: httpx2.Response) -> None:
        if response.status_code in (429, 503):
            raise self._refused(response)
        self._refusals = 0
        self._blocked_until = None
        if response.status_code == 404:
            raise RosterconNotFoundError("Rostercon has no such page.")
        if response.status_code != 200:
            raise ProviderError(f"Rostercon answered {response.status_code}.")

    def _refused(self, response: httpx2.Response) -> RosterconUnavailableError:
        self._refusals += 1
        backoff = min(self._min_interval * 2**self._refusals, MAX_COOLDOWN)
        asked = min(_retry_after(response) or 0.0, MAX_COOLDOWN)
        cooldown = max(backoff, asked)
        self._blocked_until = self._clock() + cooldown
        logger.warning(
            "rostercon_cooldown_started",
            status=response.status_code,
            cooldown_s=cooldown,
            retry_after_s=asked or None,
            refusals_in_a_row=self._refusals,
        )
        return RosterconUnavailableError(
            f"Rostercon answered {response.status_code}.",
            retry_after=timedelta(seconds=cooldown),
        )


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
