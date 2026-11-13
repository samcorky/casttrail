"""A slow, careful HTTP fetcher shared by the sources that are scraped.

Rostercon and Comiconomicon are small sites run by people, not APIs. Both are fetched
one request at a time, well spaced, and left alone for a long while when they say no.
Each source subclasses ``PoliteFetcher`` to say which host it may talk to and which
errors it raises, so a refusal by one source is never mistaken for a refusal by another.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlsplit

import httpx2
import structlog

from casttrail import get_version
from casttrail.providers.base import ProviderError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = structlog.get_logger(__name__)

# Rostercon starts answering 503 after about 25 requests in 30 minutes, so requests are
# spaced well apart and a refusal is respected for a long time, never retried at once.
MIN_INTERVAL = 10.0
MAX_COOLDOWN = 3600.0
MAX_BODY_BYTES = 5_000_000


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


@dataclass(frozen=True)
class FetchedPage:
    """A page as a source sent it."""

    text: str
    etag: str | None


def create_client(
    *, transport: httpx2.AsyncBaseTransport | None = None
) -> httpx2.AsyncClient:
    """Build the HTTP client that scraped-source requests go through.

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


class PoliteFetcher:
    """Fetches pages from one site one at a time, slowly, and backs off when told to.

    Requests are never sent faster than ``min_interval``, even from several tasks at
    once. After a 429 or 503 nothing is sent until a cooldown has passed: the longer of
    what the site asked for and a delay that doubles with each refusal in a row. Calls
    made during the cooldown fail at once without touching the network.

    A subclass sets ``source`` (a snake_case name for log lines), ``label`` (the name
    shown in error messages), ``host`` and the two error types.
    """

    source: ClassVar[str]
    label: ClassVar[str]
    host: ClassVar[str]
    unavailable_error: ClassVar[type[ProviderError]]
    not_found_error: ClassVar[type[ProviderError]]

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
            url: An ``https://`` address on this source's host.

        Returns:
            The page text and its ``ETag`` if it sent one.

        Raises:
            ProviderError: The address is not this source's, the request failed, or
                the answer was not a page we can use. The subclass's
                ``unavailable_error`` means the site refused or is being left alone;
                its ``not_found_error`` means there is no page at that address.
        """
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != self.host or parts.username:
            raise ProviderError(f"Not a {self.label} address.")
        async with self._lock:
            self._check_cooldown()
            await self._wait_turn()
            return await self._fetch(url)

    def _check_cooldown(self) -> None:
        if self._blocked_until is None:
            return
        remaining = self._blocked_until - self._clock()
        if remaining > 0:
            logger.info(
                "source_cooling_down", source=self.source, remaining_s=round(remaining)
            )
            raise self.unavailable_error(
                f"{self.label} is being left alone for now.",
                retry_after=timedelta(seconds=remaining),
            )

    async def _wait_turn(self) -> None:
        if self._last_request is not None:
            wait = self._last_request + self._min_interval - self._clock()
            if wait > 0:
                logger.debug(
                    "source_spacing", source=self.source, wait_s=round(wait, 1)
                )
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
                body = await self._read_limited(response)
                encoding = response.encoding or "utf-8"
                etag = response.headers.get("etag")
        except httpx2.HTTPError as error:
            logger.warning(
                "source_unreachable",
                source=self.source,
                path=path,
                error=type(error).__name__,
                duration_ms=_ms(started),
            )
            raise ProviderError(f"{self.label} could not be reached.") from error
        except self.not_found_error as error:
            # Expected whenever a name is not on the site, so not worth a warning.
            logger.info(
                "source_not_found",
                source=self.source,
                path=path,
                duration_ms=_ms(started),
            )
            raise error
        except ProviderError as error:
            logger.warning(
                "source_refused",
                source=self.source,
                path=path,
                status=status,
                error=str(error),
                duration_ms=_ms(started),
            )
            raise
        logger.info(
            "source_request",
            source=self.source,
            path=path,
            status=status,
            bytes=len(body),
            duration_ms=_ms(started),
        )
        return FetchedPage(text=body.decode(encoding, errors="replace"), etag=etag)

    async def _read_limited(self, response: httpx2.Response) -> bytes:
        """Read a body, giving up as soon as it passes the limit.

        The size is checked as the body arrives, on the decoded bytes, so neither a
        large ``Content-Length`` nor a small download that expands (a compression bomb)
        is read in full before being refused.
        """
        too_big = f"{self.label} sent a page larger than expected."
        declared = response.headers.get("content-length", "")
        if declared.isascii() and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            raise ProviderError(too_big)
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                raise ProviderError(too_big)
        return bytes(body)

    def _check_status(self, response: httpx2.Response) -> None:
        if response.status_code in (429, 503):
            raise self._refused(response)
        self._refusals = 0
        self._blocked_until = None
        if response.status_code == 404:
            raise self.not_found_error(f"{self.label} has no such page.")
        if response.status_code != 200:
            raise ProviderError(f"{self.label} answered {response.status_code}.")

    def _refused(self, response: httpx2.Response) -> ProviderError:
        self._refusals += 1
        backoff = min(self._min_interval * 2**self._refusals, MAX_COOLDOWN)
        asked = min(_retry_after(response) or 0.0, MAX_COOLDOWN)
        cooldown = max(backoff, asked)
        self._blocked_until = self._clock() + cooldown
        logger.warning(
            "source_cooldown_started",
            source=self.source,
            status=response.status_code,
            cooldown_s=cooldown,
            retry_after_s=asked or None,
            refusals_in_a_row=self._refusals,
        )
        return self.unavailable_error(
            f"{self.label} answered {response.status_code}.",
            retry_after=timedelta(seconds=cooldown),
        )
