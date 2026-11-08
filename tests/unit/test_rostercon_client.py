from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2
import pytest

from casttrail.providers.base import ProviderError
from casttrail.providers.rostercon_client import (
    MAX_BODY_BYTES,
    MAX_COOLDOWN,
    PeopleIndex,
    RosterconFetcher,
    RosterconNotFoundError,
    RosterconUnavailableError,
    create_client,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

FIXTURES = Path(__file__).parents[1] / "fixtures" / "rostercon"
PAGE = "https://www.rostercon.com/en/people/paul-mcgann-en"


class World:
    """A fake clock, and a fake Rostercon that records every request."""

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
        return httpx2.Response(200, text="<html></html>")

    def fetcher(self, interval: float = 10.0) -> RosterconFetcher:
        client = create_client(transport=httpx2.MockTransport(self.handler))
        return RosterconFetcher(
            client, min_interval=interval, clock=self.clock, sleep=self.sleep
        )


@pytest.fixture
def world() -> World:
    return World()


# --- requests --------------------------------------------------------------------


async def test_a_page_comes_back_with_its_text_and_etag(world: World) -> None:
    world.responses = [httpx2.Response(200, text="<p>hi</p>", headers={"ETag": '"x"'})]

    page = await world.fetcher().get(PAGE)

    assert (page.text, page.etag) == ("<p>hi</p>", '"x"')


async def test_a_page_without_an_etag_has_none(world: World) -> None:
    page = await world.fetcher().get(PAGE)

    assert page.etag is None


async def test_the_client_says_who_it_is_and_does_not_follow_redirects(
    world: World,
) -> None:
    world.responses = [httpx2.Response(302, headers={"Location": "https://evil.test/"})]

    with pytest.raises(ProviderError):
        await world.fetcher().get(PAGE)

    assert world.requests[0].headers["user-agent"].startswith("CastTrail/")
    assert len(world.requests) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://www.rostercon.com/en/people/x",
        "https://rostercon.com/en/people/x",
        "https://www.rostercon.com.evil.test/en/people/x",
        "https://user@www.rostercon.com/en/people/x",
        "https://evil.test/www.rostercon.com",
        "file:///etc/passwd",
        "",
    ],
)
async def test_only_rostercon_over_https_is_ever_asked(world: World, url: str) -> None:
    with pytest.raises(ProviderError):
        await world.fetcher().get(url)

    assert world.requests == []


# --- spacing ---------------------------------------------------------------------


async def test_the_first_request_goes_at_once(world: World) -> None:
    await world.fetcher().get(PAGE)

    assert world.slept == []


async def test_requests_are_spaced_by_the_interval(world: World) -> None:
    fetcher = world.fetcher(10.0)
    await fetcher.get(PAGE)
    world.now += 3

    await fetcher.get(PAGE)

    assert world.slept == [7.0]


async def test_no_wait_is_needed_once_the_interval_has_passed(world: World) -> None:
    fetcher = world.fetcher(10.0)
    await fetcher.get(PAGE)
    world.now += 10

    await fetcher.get(PAGE)

    assert world.slept == []


async def test_a_failed_request_still_counts_for_spacing(world: World) -> None:
    fetcher = world.fetcher(10.0)
    world.responses = [httpx2.Response(500)]
    with pytest.raises(ProviderError):
        await fetcher.get(PAGE)

    await fetcher.get(PAGE)

    assert world.slept == [10.0]


# --- backing off -----------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 503])
async def test_a_refusal_starts_a_cooldown_and_nothing_is_sent_during_it(
    world: World, status: int
) -> None:
    fetcher = world.fetcher(10.0)
    world.responses = [httpx2.Response(status)]
    with pytest.raises(RosterconUnavailableError) as refused:
        await fetcher.get(PAGE)
    assert refused.value.retry_after == timedelta(seconds=20)

    world.now += 5
    with pytest.raises(RosterconUnavailableError) as waiting:
        await fetcher.get(PAGE)

    assert len(world.requests) == 1
    assert waiting.value.retry_after == timedelta(seconds=15)


async def test_requests_resume_after_the_cooldown(world: World) -> None:
    fetcher = world.fetcher(10.0)
    world.responses = [httpx2.Response(503)]
    with pytest.raises(RosterconUnavailableError):
        await fetcher.get(PAGE)
    world.now += 20

    page = await fetcher.get(PAGE)

    assert page.text == "<html></html>"
    assert len(world.requests) == 2


async def test_what_rostercon_asks_for_wins_when_it_is_longer(world: World) -> None:
    world.responses = [httpx2.Response(429, headers={"Retry-After": "300"})]

    with pytest.raises(RosterconUnavailableError) as refused:
        await world.fetcher(10.0).get(PAGE)

    assert refused.value.retry_after == timedelta(seconds=300)


async def test_our_own_backoff_wins_when_it_is_longer(world: World) -> None:
    world.responses = [httpx2.Response(429, headers={"Retry-After": "1"})]

    with pytest.raises(RosterconUnavailableError) as refused:
        await world.fetcher(10.0).get(PAGE)

    assert refused.value.retry_after == timedelta(seconds=20)


@pytest.mark.parametrize(
    "header", ["Wed, 21 Oct 2026 07:28:00 GMT", "soon", "-5", "", "1e9", "٣٠٠"]
)
async def test_a_retry_after_that_is_not_plain_seconds_is_ignored(
    world: World, header: str
) -> None:
    world.responses = [
        httpx2.Response(503, headers=[(b"retry-after", header.encode())])
    ]

    with pytest.raises(RosterconUnavailableError) as refused:
        await world.fetcher(10.0).get(PAGE)

    assert refused.value.retry_after == timedelta(seconds=20)


async def test_the_cooldown_doubles_with_each_refusal_in_a_row(world: World) -> None:
    fetcher = world.fetcher(10.0)
    waits = []
    for _ in range(3):
        world.responses = [httpx2.Response(503)]
        with pytest.raises(RosterconUnavailableError) as refused:
            await fetcher.get(PAGE)
        assert refused.value.retry_after is not None
        waits.append(refused.value.retry_after.total_seconds())
        world.now += refused.value.retry_after.total_seconds()

    assert waits == [20.0, 40.0, 80.0]


async def test_a_success_resets_the_doubling(world: World) -> None:
    fetcher = world.fetcher(10.0)
    world.responses = [httpx2.Response(503)]
    with pytest.raises(RosterconUnavailableError):
        await fetcher.get(PAGE)
    world.now += 20
    await fetcher.get(PAGE)

    world.responses = [httpx2.Response(503)]
    with pytest.raises(RosterconUnavailableError) as refused:
        await fetcher.get(PAGE)

    assert refused.value.retry_after == timedelta(seconds=20)


async def test_the_cooldown_is_capped(world: World) -> None:
    fetcher = world.fetcher(10.0)
    world.responses = [httpx2.Response(503, headers={"Retry-After": "999999999"})]

    with pytest.raises(RosterconUnavailableError) as refused:
        await fetcher.get(PAGE)

    assert refused.value.retry_after == timedelta(seconds=MAX_COOLDOWN)


async def test_doubling_stops_at_the_cap(world: World) -> None:
    fetcher = world.fetcher(10.0)
    for _ in range(12):
        world.responses = [httpx2.Response(503)]
        with pytest.raises(RosterconUnavailableError) as refused:
            await fetcher.get(PAGE)
        assert refused.value.retry_after is not None
        world.now += refused.value.retry_after.total_seconds()

    assert refused.value.retry_after == timedelta(seconds=MAX_COOLDOWN)


# --- other answers ---------------------------------------------------------------


async def test_a_missing_page_is_not_found_and_does_not_start_a_cooldown(
    world: World,
) -> None:
    fetcher = world.fetcher(10.0)
    world.responses = [httpx2.Response(404)]
    with pytest.raises(RosterconNotFoundError):
        await fetcher.get(PAGE)

    await fetcher.get(PAGE)

    assert world.slept == [10.0]


@pytest.mark.parametrize("status", [301, 302, 403, 410, 500, 502])
async def test_any_other_answer_is_an_error_without_a_cooldown(
    world: World, status: int
) -> None:
    fetcher = world.fetcher(10.0)
    world.responses = [httpx2.Response(status)]

    with pytest.raises(ProviderError) as failed:
        await fetcher.get(PAGE)

    assert not isinstance(failed.value, RosterconUnavailableError)
    assert str(status) in str(failed.value)
    await fetcher.get(PAGE)


async def test_a_network_failure_is_a_provider_error(world: World) -> None:
    def broken(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("boom", request=request)

    client = create_client(transport=httpx2.MockTransport(broken))
    fetcher = RosterconFetcher(client, clock=world.clock, sleep=world.sleep)

    with pytest.raises(ProviderError, match="could not be reached") as failed:
        await fetcher.get(PAGE)

    assert "boom" not in str(failed.value)
    assert "rostercon.com" not in str(failed.value)


async def test_an_oversized_page_is_refused(world: World) -> None:
    world.responses = [httpx2.Response(200, content=b"x" * (MAX_BODY_BYTES + 1))]

    with pytest.raises(ProviderError, match="larger"):
        await world.fetcher().get(PAGE)


async def test_a_page_of_exactly_the_limit_is_accepted(world: World) -> None:
    world.responses = [httpx2.Response(200, content=b"x" * MAX_BODY_BYTES)]

    page = await world.fetcher().get(PAGE)

    assert len(page.text) == MAX_BODY_BYTES


# --- the people index ------------------------------------------------------------

_SITEMAP = (FIXTURES / "sitemap_people_sample.xml").read_text(encoding="utf-8")


def _sitemap_with(*slugs: str) -> str:
    return "".join(
        f"<loc>https://www.rostercon.com/en/people/{slug}</loc>" for slug in slugs
    )


def _serve_sitemaps(world: World, files: dict[str, str]) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        world.requests.append(request)
        name = request.url.path.removeprefix("/")
        if name in files:
            return httpx2.Response(200, text=files[name])
        return httpx2.Response(404)

    world.handler = handler  # type: ignore[method-assign]


def _index(world: World, lifetime: timedelta = timedelta(days=14)) -> PeopleIndex:
    return PeopleIndex(world.fetcher(10.0), lifetime=lifetime, clock=world.clock)


async def test_the_index_reads_files_until_one_is_missing(world: World) -> None:
    _serve_sitemaps(
        world,
        {
            "acteurs-sitemap.xml": _sitemap_with("a-en"),
            "acteurs-sitemap2.xml": _sitemap_with("b-en"),
            "acteurs-sitemap3.xml": _sitemap_with("c-en"),
        },
    )

    people = await _index(world).people()

    assert [person.slug for person in people] == ["a-en", "b-en", "c-en"]
    assert [r.url.path for r in world.requests] == [
        "/acteurs-sitemap.xml",
        "/acteurs-sitemap2.xml",
        "/acteurs-sitemap3.xml",
        "/acteurs-sitemap4.xml",
    ]


async def test_the_real_sample_fixture_yields_people(world: World) -> None:
    _serve_sitemaps(world, {"acteurs-sitemap.xml": _SITEMAP})

    people = await _index(world).people()

    assert len(people) > 5


async def test_a_person_in_two_files_is_listed_once(world: World) -> None:
    _serve_sitemaps(
        world,
        {
            "acteurs-sitemap.xml": _sitemap_with("a-en", "b-en"),
            "acteurs-sitemap2.xml": _sitemap_with("b-en", "c-en"),
        },
    )

    people = await _index(world).people()

    assert [person.slug for person in people] == ["a-en", "b-en", "c-en"]


async def test_a_missing_first_file_is_an_error(world: World) -> None:
    _serve_sitemaps(world, {})

    with pytest.raises(RosterconNotFoundError):
        await _index(world).people()


async def test_a_file_with_no_people_is_an_error_and_is_not_cached(
    world: World,
) -> None:
    _serve_sitemaps(world, {"acteurs-sitemap.xml": "<urlset></urlset>"})
    index = _index(world)

    with pytest.raises(ProviderError, match="no one"):
        await index.people()
    with pytest.raises(ProviderError, match="no one"):
        await index.people()

    assert len(world.requests) == 2


async def test_a_complete_index_is_reused_until_it_expires(world: World) -> None:
    _serve_sitemaps(world, {"acteurs-sitemap.xml": _sitemap_with("a-en")})
    index = _index(world, timedelta(days=14))
    await index.people()
    count = len(world.requests)

    world.now += timedelta(days=14).total_seconds() - 1
    await index.people()
    assert len(world.requests) == count

    world.now += 1
    await index.people()
    assert len(world.requests) > count


async def test_an_expired_index_is_replaced_not_added_to(world: World) -> None:
    files = {"acteurs-sitemap.xml": _sitemap_with("a-en")}
    _serve_sitemaps(world, files)
    index = _index(world, timedelta(days=1))
    await index.people()

    files["acteurs-sitemap.xml"] = _sitemap_with("z-en")
    world.now += timedelta(days=1).total_seconds()
    people = await index.people()

    assert [person.slug for person in people] == ["z-en"]


async def test_a_refusal_part_way_keeps_the_files_already_read(world: World) -> None:
    files = {
        "acteurs-sitemap.xml": _sitemap_with("a-en"),
        "acteurs-sitemap2.xml": _sitemap_with("b-en"),
    }
    calls = {"count": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        world.requests.append(request)
        name = request.url.path.removeprefix("/")
        if name == "acteurs-sitemap2.xml":
            calls["count"] += 1
            if calls["count"] == 1:
                return httpx2.Response(503)
        if name in files:
            return httpx2.Response(200, text=files[name])
        return httpx2.Response(404)

    world.handler = handler  # type: ignore[method-assign]
    index = _index(world)
    with pytest.raises(RosterconUnavailableError):
        await index.people()
    world.now += 100

    people = await index.people()

    assert [person.slug for person in people] == ["a-en", "b-en"]
    paths = [request.url.path for request in world.requests]
    assert paths.count("/acteurs-sitemap.xml") == 1


async def test_the_index_stops_at_a_fixed_number_of_files(world: World) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        world.requests.append(request)
        return httpx2.Response(200, text=_sitemap_with(f"p{len(world.requests)}-en"))

    world.handler = handler  # type: ignore[method-assign]

    people = await _index(world).people()

    assert len(world.requests) == 20
    assert len(people) == 20


class _Endless(httpx2.AsyncByteStream):
    """A body that never ends, so only the size limit can stop the read."""

    def __init__(self) -> None:
        self.sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            self.sent += 1
            yield b"x" * 100_000


async def test_a_body_that_never_ends_is_cut_off_at_the_limit(world: World) -> None:
    body = _Endless()
    world.responses = [httpx2.Response(200, stream=body)]

    with pytest.raises(ProviderError, match="larger"):
        await world.fetcher().get(PAGE)

    assert body.sent <= MAX_BODY_BYTES // 100_000 + 2


async def test_a_declared_length_over_the_limit_is_refused_before_reading(
    world: World,
) -> None:
    body = _Endless()
    world.responses = [
        httpx2.Response(
            200, stream=body, headers={"Content-Length": str(MAX_BODY_BYTES + 1)}
        )
    ]

    with pytest.raises(ProviderError, match="larger"):
        await world.fetcher().get(PAGE)

    assert body.sent == 0


async def test_a_page_is_decoded_with_the_charset_it_declares(world: World) -> None:
    world.responses = [
        httpx2.Response(
            200,
            content="café".encode("latin-1"),
            headers={"Content-Type": "text/html; charset=latin-1"},
        )
    ]

    page = await world.fetcher().get(PAGE)

    assert page.text == "café"


async def test_bytes_that_do_not_decode_are_replaced_not_fatal(world: World) -> None:
    world.responses = [
        httpx2.Response(
            200, content=b"\xff\xfe ok", headers={"Content-Type": "text/html"}
        )
    ]

    page = await world.fetcher().get(PAGE)

    assert page.text.endswith(" ok")
