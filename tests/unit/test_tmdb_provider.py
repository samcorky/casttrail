import json
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2
import pytest

from casttrail.providers.base import MetadataProvider, ProviderError
from casttrail.providers.tmdb import TmdbProvider, create_client

if TYPE_CHECKING:
    from collections.abc import Callable

FIXTURES = Path(__file__).parents[1] / "fixtures" / "tmdb"
TOKEN = "test-read-token-1234567890"

# (path, query term) -> fixture, for the requests the provider is expected to make.
ROUTES: dict[tuple[str, str | None], str] = {
    ("/3/search/person", "Eddie McClintock"): "search_person_eddie_mcclintock",
    ("/3/search/tv", "Warehouse 13"): "search_tv_warehouse_13",
    ("/3/search/movie", "Warehouse 13"): "search_movie_skyfall",
    ("/3/person/120679", None): "person_120679",
    ("/3/movie/37724", None): "movie_37724",
    ("/3/tv/15819", None): "tv_15819",
    ("/3/person/120679/combined_credits", None): "person_120679_combined_credits",
    ("/3/tv/15819/aggregate_credits", None): "tv_15819_aggregate_credits",
    ("/3/movie/37724/credits", None): "movie_37724_credits",
}


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class Recorder:
    """A fake TMDB that serves the saved fixtures and remembers what was asked."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        key = (request.url.path, request.url.params.get("query"))
        name = ROUTES.get(key)
        if name is None:
            return httpx2.Response(404, json=_fixture("error_not_found"))
        return httpx2.Response(200, json=_fixture(name))


def _provider(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> TmdbProvider:
    return TmdbProvider(create_client(TOKEN, transport=httpx2.MockTransport(handler)))


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def tmdb(recorder: Recorder) -> TmdbProvider:
    return _provider(recorder)


def test_the_provider_satisfies_the_metadata_protocol(tmdb: TmdbProvider) -> None:
    provider: MetadataProvider = tmdb

    assert provider.provider_id == "tmdb"


async def test_requests_carry_the_token_in_a_header_and_never_in_the_url(
    tmdb: TmdbProvider, recorder: Recorder
) -> None:
    await tmdb.search_people("Eddie McClintock")

    request = recorder.requests[0]
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Accept"] == "application/json"
    assert request.method == "GET"
    assert str(request.url).startswith("https://api.themoviedb.org/3/search/person")
    assert TOKEN not in str(request.url)
    assert "api_key" not in str(request.url)


# --- search ---------------------------------------------------------------------


async def test_search_people_returns_what_tmdb_found(tmdb: TmdbProvider) -> None:
    people = await tmdb.search_people("Eddie McClintock")

    assert len(people) == 3
    first = people[0]
    assert (first.tmdb_id, first.name) == (120679, "Eddie McClintock")
    assert first.known_for_department == "Acting"


@pytest.mark.parametrize("query", ["", "   ", "\t\n"])
async def test_a_blank_search_asks_nothing(
    tmdb: TmdbProvider, recorder: Recorder, query: str
) -> None:
    assert await tmdb.search_people(query) == []
    assert await tmdb.search_productions(query) == []
    assert recorder.requests == []


async def test_search_trims_the_query_and_excludes_adult_results(
    tmdb: TmdbProvider, recorder: Recorder
) -> None:
    await tmdb.search_people("  Eddie McClintock ")

    params = recorder.requests[0].url.params
    assert params["query"] == "Eddie McClintock"
    assert params["include_adult"] == "false"


async def test_search_productions_returns_films_then_series(
    tmdb: TmdbProvider, recorder: Recorder
) -> None:
    productions = await tmdb.search_productions("Warehouse 13")

    assert {r.url.path for r in recorder.requests} == {
        "/3/search/movie",
        "/3/search/tv",
    }
    kinds = [p.media_type for p in productions]
    assert kinds == sorted(kinds, key={"movie": 0, "tv": 1}.__getitem__)
    series = next(p for p in productions if p.media_type == "tv")
    assert (series.tmdb_id, series.title, series.release_year) == (
        15819,
        "Warehouse 13",
        2009,
    )
    film = next(p for p in productions if p.media_type == "movie")
    assert film.title == "Skyfall"


# --- one production -------------------------------------------------------------


async def test_a_film_is_looked_up_by_its_own_id(
    tmdb: TmdbProvider, recorder: Recorder
) -> None:
    film = await tmdb.production("movie", 37724)

    assert recorder.requests[0].url.path == "/3/movie/37724"
    assert film is not None
    assert (film.media_type, film.tmdb_id, film.title) == ("movie", 37724, "Skyfall")
    assert film.release_year == 2012
    assert film.poster_path == "/d0IVecFQvsGdSbnMAHqiYsNYaJT.jpg"


async def test_a_series_is_looked_up_by_its_own_id(
    tmdb: TmdbProvider, recorder: Recorder
) -> None:
    show = await tmdb.production("tv", 15819)

    assert recorder.requests[0].url.path == "/3/tv/15819"
    assert show is not None
    assert (show.media_type, show.title, show.release_year) == (
        "tv",
        "Warehouse 13",
        2009,
    )


async def test_the_same_number_is_a_different_thing_for_a_film_and_a_series(
    tmdb: TmdbProvider,
) -> None:
    assert await tmdb.production("tv", 37724) is None
    assert await tmdb.production("movie", 15819) is None


async def test_an_unknown_film_or_series_is_none(tmdb: TmdbProvider) -> None:
    assert await tmdb.production("movie", 1) is None
    assert await tmdb.production("tv", 1) is None


async def test_a_production_with_no_title_is_an_error() -> None:
    provider = _provider(lambda _request: httpx2.Response(200, json={"id": 1}))

    with pytest.raises(ProviderError, match="response format"):
        await provider.production("movie", 1)


# --- person ---------------------------------------------------------------------


async def test_person_returns_the_imdb_id(tmdb: TmdbProvider) -> None:
    person = await tmdb.person(120679)

    assert person is not None
    assert (person.tmdb_id, person.name) == (120679, "Eddie McClintock")
    assert person.imdb_id == "nm0565858"


async def test_an_unknown_person_is_none(tmdb: TmdbProvider) -> None:
    assert await tmdb.person(1) is None
    assert await tmdb.person_credits(1) is None


@pytest.mark.parametrize("imdb_id", [None, "", "  "])
async def test_a_missing_imdb_id_is_none(imdb_id: str | None) -> None:
    provider = _provider(
        lambda _request: httpx2.Response(
            200, json={"id": 5, "name": "A Person", "imdb_id": imdb_id}
        )
    )

    person = await provider.person(5)

    assert person is not None
    assert person.imdb_id is None


async def test_person_credits_mix_films_and_series(tmdb: TmdbProvider) -> None:
    credits = await tmdb.person_credits(120679)

    assert credits is not None
    assert {c.production.media_type for c in credits} == {"movie", "tv"}
    series = next(c for c in credits if c.production.media_type == "tv")
    assert series.production.title == "Less than Perfect"
    assert series.production.release_year == 2002
    assert series.character == "Jake Heberling"
    film = next(c for c in credits if c.production.title == "Three Wise Guys")
    assert (film.production.release_year, film.character, film.order) == (
        2005,
        "Joey",
        0,
    )


async def test_credits_keep_gaps_as_none(tmdb: TmdbProvider) -> None:
    credits = await tmdb.person_credits(120679)

    assert credits is not None
    undated = next(c for c in credits if c.production.title == "Disorientated")
    assert undated.production.release_year is None
    blank_character = next(c for c in credits if c.production.title == "Greg the Bunny")
    assert blank_character.character is None
    no_order = next(c for c in credits if c.production.media_type == "tv")
    assert no_order.order is None


async def test_a_credit_with_no_title_is_left_out() -> None:
    body = {
        "cast": [
            {"id": 1, "media_type": "movie", "title": "Real Film"},
            {"id": 2, "media_type": "movie", "title": ""},
            {"id": 3, "media_type": "tv"},
        ]
    }
    provider = _provider(lambda _request: httpx2.Response(200, json=body))

    credits = await provider.person_credits(9)

    assert credits is not None
    assert [c.production.title for c in credits] == ["Real Film"]


# --- cast -----------------------------------------------------------------------


async def test_a_series_cast_comes_from_the_aggregate_credits(
    tmdb: TmdbProvider, recorder: Recorder
) -> None:
    cast = await tmdb.cast("tv", 15819)

    assert recorder.requests[0].url.path == "/3/tv/15819/aggregate_credits"
    assert cast is not None
    assert len(cast) == 10
    assert [m.order for m in cast] == sorted(m.order for m in cast)
    lead = cast[0]
    assert (lead.person.tmdb_id, lead.person.name) == (120679, "Eddie McClintock")
    assert lead.character == "Pete Lattimer"


async def test_a_film_cast_comes_from_its_credits(
    tmdb: TmdbProvider, recorder: Recorder
) -> None:
    cast = await tmdb.cast("movie", 37724, limit=3)

    assert recorder.requests[0].url.path == "/3/movie/37724/credits"
    assert cast is not None
    assert len(cast) == 3
    assert [m.order for m in cast] == [0, 1, 2]
    assert cast[0].person.name == "Daniel Craig"
    assert cast[0].character == "James Bond"


@pytest.mark.parametrize(("limit", "expected"), [(0, 0), (-1, 0), (500, 12)])
async def test_the_cast_limit_is_clamped(
    tmdb: TmdbProvider, limit: int, expected: int
) -> None:
    cast = await tmdb.cast("tv", 15819, limit=limit)

    assert cast is not None
    assert len(cast) == expected


async def test_the_cast_is_sorted_by_billing_whatever_order_tmdb_sent() -> None:
    body = {
        "cast": [
            {"id": 2, "name": "Second", "order": 1},
            {"id": 1, "name": "First", "order": 0},
        ]
    }
    provider = _provider(lambda _request: httpx2.Response(200, json=body))

    cast = await provider.cast("movie", 1)

    assert cast is not None
    assert [m.person.name for m in cast] == ["First", "Second"]


async def test_a_series_character_skips_blank_roles() -> None:
    body = {
        "cast": [
            {
                "id": 1,
                "name": "A",
                "order": 0,
                "roles": [{"character": ""}, {"character": "Real Role"}],
            }
        ]
    }
    provider = _provider(lambda _request: httpx2.Response(200, json=body))

    cast = await provider.cast("tv", 1)

    assert cast is not None
    assert cast[0].character == "Real Role"


async def test_an_unknown_production_is_none(tmdb: TmdbProvider) -> None:
    assert await tmdb.cast("tv", 1) is None
    assert await tmdb.cast("movie", 1) is None


# --- failures -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "rejected the credentials"),
        (403, "rejected the credentials"),
        (429, "limiting requests"),
        (500, "HTTP 500"),
        (503, "HTTP 503"),
    ],
)
async def test_http_failures_become_provider_errors(status: int, message: str) -> None:
    provider = _provider(lambda _request: httpx2.Response(status, json={}))

    with pytest.raises(ProviderError, match=message) as raised:
        await provider.search_people("anyone")

    assert TOKEN not in str(raised.value)


async def test_a_rate_limit_carries_how_long_to_wait() -> None:
    provider = _provider(
        lambda _request: httpx2.Response(429, headers={"Retry-After": "7"})
    )

    with pytest.raises(ProviderError) as raised:
        await provider.search_people("anyone")

    assert raised.value.retry_after == timedelta(seconds=7)


@pytest.mark.parametrize("header", [{}, {"Retry-After": "soon"}])
async def test_an_unreadable_retry_after_is_none(header: dict[str, str]) -> None:
    provider = _provider(lambda _request: httpx2.Response(429, headers=header))

    with pytest.raises(ProviderError) as raised:
        await provider.search_people("anyone")

    assert raised.value.retry_after is None


async def test_a_network_failure_becomes_a_provider_error() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        msg = f"connection refused to {request.url}"
        raise httpx2.ConnectError(msg)

    with pytest.raises(ProviderError, match="Could not reach TMDB") as raised:
        await _provider(refuse).search_people("anyone")

    assert TOKEN not in str(raised.value)
    assert "themoviedb" not in str(raised.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(200, content=b"<html>not json</html>"),
        httpx2.Response(200, json=["a", "list"]),
        httpx2.Response(200, json={"page": 1}),
        httpx2.Response(200, json={"results": "nope"}),
        httpx2.Response(200, json={"results": [{"id": "abc", "name": "A"}]}),
        httpx2.Response(200, json={"results": [{"id": 1}]}),
    ],
)
async def test_a_changed_response_format_is_an_error_not_a_guess(
    response: httpx2.Response,
) -> None:
    provider = _provider(lambda _request: response)

    with pytest.raises(ProviderError, match=r"not JSON|response format"):
        await provider.search_people("anyone")


async def test_an_unexpected_media_type_is_an_error() -> None:
    body = {"cast": [{"id": 1, "media_type": "person", "title": "X"}]}
    provider = _provider(lambda _request: httpx2.Response(200, json=body))

    with pytest.raises(ProviderError, match="response format"):
        await provider.person_credits(1)


async def test_a_missing_billing_order_in_a_cast_is_an_error() -> None:
    body = {"cast": [{"id": 1, "name": "A"}]}
    provider = _provider(lambda _request: httpx2.Response(200, json=body))

    with pytest.raises(ProviderError, match="response format"):
        await provider.cast("movie", 1)


async def test_the_data_classes_are_immutable(tmdb: TmdbProvider) -> None:
    person = (await tmdb.search_people("Eddie McClintock"))[0]

    with pytest.raises(ValueError, match="frozen"):
        person.name = "Someone Else"  # type: ignore[misc]
