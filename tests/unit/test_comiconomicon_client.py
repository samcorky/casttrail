import json
from pathlib import Path

import httpx2
import pytest
import structlog

from casttrail.providers.base import ProviderError
from casttrail.providers.comiconomicon_client import (
    ComiconomiconFetcher,
    ComiconomiconNotFoundError,
    ComiconomiconUnavailableError,
    find_guests,
    search,
)
from casttrail.providers.polite import create_client

FIXTURES = Path(__file__).parents[1] / "fixtures" / "comiconomicon"
GUEST_ANSWER = (FIXTURES / "autocomplete_guest_andrew_jackson.json").read_text()


class World:
    """A fake clock, and a fake Comiconomicon that records every request."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []
        self.requests: list[httpx2.Request] = []
        self.responses: list[httpx2.Response] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        return httpx2.Response(200, text="[]")

    def fetcher(self) -> ComiconomiconFetcher:
        client = create_client(transport=httpx2.MockTransport(self.handler))
        return ComiconomiconFetcher(
            client, min_interval=10.0, clock=self.clock, sleep=self.sleep
        )


@pytest.fixture
def world() -> World:
    return World()


# --- the shared rules apply to this source too -----------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://comiconomicon.com/xhr/indexautocomplete.php",
        "https://www.comiconomicon.com/xhr/indexautocomplete.php",
        "https://comiconomicon.com.evil.test/xhr/indexautocomplete.php",
        "https://user@comiconomicon.com/xhr/indexautocomplete.php",
        "https://www.rostercon.com/en/people/x",
        "",
    ],
)
async def test_only_comiconomicon_over_https_is_ever_asked(
    world: World, url: str
) -> None:
    with pytest.raises(ProviderError, match="Not a Comiconomicon address"):
        await world.fetcher().get(url)

    assert world.requests == []


async def test_a_refusal_is_this_sources_own_error_and_starts_its_own_cooldown(
    world: World,
) -> None:
    world.responses = [httpx2.Response(503)]
    fetcher = world.fetcher()

    with pytest.raises(ComiconomiconUnavailableError, match="Comiconomicon answered"):
        await search(fetcher, "Andrew Jackson")
    with pytest.raises(ComiconomiconUnavailableError, match="left alone"):
        await search(fetcher, "Andrew Jackson")

    assert len(world.requests) == 1


async def test_a_missing_page_is_this_sources_own_not_found_error(
    world: World,
) -> None:
    world.responses = [httpx2.Response(404)]

    with pytest.raises(ComiconomiconNotFoundError, match="Comiconomicon has no"):
        await search(world.fetcher(), "Andrew Jackson")


async def test_requests_are_spaced_ten_seconds_apart(world: World) -> None:
    fetcher = world.fetcher()

    await search(fetcher, "Andrew Jackson")
    await search(fetcher, "Someone Else")

    assert world.slept == [10.0]


# --- searching -------------------------------------------------------------------


async def test_a_name_is_sent_as_the_term_and_encoded(world: World) -> None:
    await search(world.fetcher(), "  Pierce   Brosnan & Co  ")

    request = world.requests[0]
    assert request.url.path == "/xhr/indexautocomplete.php"
    assert request.url.params["term"] == "Pierce Brosnan & Co"
    # The ampersand is escaped, so it cannot start a second parameter.
    assert request.url.query == b"term=Pierce+Brosnan+%26+Co"


@pytest.mark.parametrize("term", ["", "   ", "x" * 101])
async def test_an_empty_or_overlong_name_sends_nothing(world: World, term: str) -> None:
    found = await search(world.fetcher(), term)

    assert (found.items, found.skipped) == ([], 0)
    assert world.requests == []


async def test_a_search_gives_the_parsed_results(world: World) -> None:
    world.responses = [httpx2.Response(200, text=GUEST_ANSWER)]

    found = await search(world.fetcher(), "Andrew Jackson")

    assert [(i.kind, i.source_key, i.name) for i in found.items] == [
        ("guest", "518", "Andrew Jackson")
    ]


async def test_an_answer_that_is_not_a_list_is_an_error_not_no_results(
    world: World,
) -> None:
    world.responses = [httpx2.Response(200, text=json.dumps({"error": "x"}))]

    with pytest.raises(ProviderError, match="did not answer with a list"):
        await search(world.fetcher(), "Andrew Jackson")


async def test_the_name_searched_for_never_reaches_the_log(world: World) -> None:
    with structlog.testing.capture_logs() as events:
        await search(world.fetcher(), "Andrew Jackson")

    assert events
    assert "Andrew" not in json.dumps(events, default=str)
    assert [e["source"] for e in events if e["event"] == "source_request"] == [
        "comiconomicon"
    ]


# --- finding a guest -------------------------------------------------------------


async def test_a_guest_with_exactly_that_name_is_found(world: World) -> None:
    world.responses = [httpx2.Response(200, text=GUEST_ANSWER)]

    found = await find_guests(world.fetcher(), "andrew jackson")

    assert [g.source_key for g in found] == ["518"]


async def test_a_different_name_finds_no_one(world: World) -> None:
    world.responses = [httpx2.Response(200, text=GUEST_ANSWER)]

    assert await find_guests(world.fetcher(), "Andrew Jacksonville") == []


async def test_an_error_is_not_mistaken_for_not_found(world: World) -> None:
    world.responses = [httpx2.Response(500)]

    with pytest.raises(ProviderError):
        await find_guests(world.fetcher(), "Andrew Jackson")
