import asyncio
import time
from datetime import timedelta
from typing import Any

import httpx2
import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from casttrail.providers.base import (
    CastMember,
    MediaType,
    PersonCredit,
    PersonDetail,
    PersonSummary,
    Production,
    ProviderError,
)

logger = structlog.get_logger(__name__)

BASE_URL = "https://api.themoviedb.org/3"
IMAGE_BASE_URL = "https://image.tmdb.org/t/p"


def create_client(
    read_token: str, *, transport: httpx2.AsyncBaseTransport | None = None
) -> httpx2.AsyncClient:
    """Build the HTTP client TMDB requests go through.

    The token travels in a header, so it never appears in a URL or a request log.

    Args:
        read_token: TMDB's "API Read Access Token".
        transport: Replaces the network. Tests pass a mock here.

    Returns:
        A client the caller must close.
    """
    return httpx2.AsyncClient(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {read_token}", "Accept": "application/json"},
        timeout=httpx2.Timeout(10.0),
        transport=transport,
    )


# What TMDB sends. Anything we do not list is ignored, and anything we list but cannot
# read is a ``ProviderError``: imported data is untrusted and a changed format should be
# noticed, not guessed at.
def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _WirePerson(_Wire):
    id: int
    name: str
    known_for_department: str | None = None
    profile_path: str | None = None


class _WirePersonDetail(_WirePerson):
    imdb_id: str | None = None


class _WireProduction(_Wire):
    id: int
    title: str | None = None
    name: str | None = None
    release_date: str | None = None
    first_air_date: str | None = None
    overview: str | None = None
    poster_path: str | None = None


class _WireCredit(_WireProduction):
    media_type: MediaType
    character: str | None = None
    order: int | None = None


class _WireRole(_Wire):
    character: str | None = None


class _WireTvCast(_WirePerson):
    order: int
    roles: list[_WireRole] = []


class _WireMovieCast(_WirePerson):
    order: int
    character: str | None = None


class _WireResults[T: _Wire](_Wire):
    results: list[T]


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


def _text(value: str | None) -> str | None:
    """TMDB sends an empty string for "unknown"; the rest of the app uses ``None``."""
    return value.strip() or None if value else None


def _person(wire: _WirePerson) -> PersonSummary:
    return PersonSummary(
        tmdb_id=wire.id,
        name=wire.name,
        known_for_department=_text(wire.known_for_department),
        profile_path=_text(wire.profile_path),
    )


def _production(wire: _WireProduction, media_type: MediaType) -> Production | None:
    """Convert a film or series, or ``None`` if TMDB gave it no title at all."""
    title = _text(wire.title) or _text(wire.name)
    if title is None:
        return None
    date = wire.release_date if media_type == "movie" else wire.first_air_date
    return Production(
        tmdb_id=wire.id,
        media_type=media_type,
        title=title,
        release_year=_year(date),
        overview=_text(wire.overview),
        poster_path=_text(wire.poster_path),
    )


class TmdbProvider:
    """People, films and series from The Movie Database."""

    provider_id = "tmdb"

    def __init__(self, client: httpx2.AsyncClient) -> None:
        """Create the provider.

        Args:
            client: A client from ``create_client``. The caller closes it.
        """
        self._client = client

    async def search_people(self, query: str) -> list[PersonSummary]:
        """Find people by name.

        Args:
            query: What was typed. A blank query finds nothing and asks nothing.

        Returns:
            Matches in TMDB's order.
        """
        if not query.strip():
            return []
        data = await self._get(
            "search/person", {"query": query.strip(), "include_adult": "false"}
        )
        found = self._parse(_WireResults[_WirePerson], data)
        return [_person(wire) for wire in found.results]

    async def search_productions(self, query: str) -> list[Production]:
        """Find films and series by title.

        Args:
            query: What was typed. A blank query finds nothing and asks nothing.

        Returns:
            Films first, then series, each in TMDB's order.
        """
        if not query.strip():
            return []
        params = {"query": query.strip(), "include_adult": "false"}
        films, series = await asyncio.gather(
            self._get("search/movie", params), self._get("search/tv", params)
        )
        productions = [
            *self._productions(films, "movie"),
            *self._productions(series, "tv"),
        ]
        return productions

    async def production(
        self, media_type: MediaType, tmdb_id: int
    ) -> Production | None:
        """Return one film or series.

        Args:
            media_type: Whether it is a film or a series.
            tmdb_id: TMDB's id for it.

        Returns:
            The production, or ``None`` if TMDB has no such film or series.

        Raises:
            ProviderError: If TMDB answers with something that has no title.
        """
        data = await self._get(f"{media_type}/{tmdb_id}", allow_missing=True)
        if data is None:
            return None
        production = _production(self._parse(_WireProduction, data), media_type)
        if production is None:
            msg = "TMDB changed its response format."
            raise ProviderError(msg)
        return production

    async def person(self, tmdb_id: int) -> PersonDetail | None:
        """Return one person with their IMDb id.

        Args:
            tmdb_id: TMDB's id for the person.

        Returns:
            The person, or ``None`` if TMDB has no such person.
        """
        data = await self._get(f"person/{tmdb_id}", allow_missing=True)
        if data is None:
            return None
        wire = self._parse(_WirePersonDetail, data)
        return PersonDetail(**_person(wire).model_dump(), imdb_id=_text(wire.imdb_id))

    async def person_credits(self, tmdb_id: int) -> list[PersonCredit] | None:
        """Return the films and series a person has appeared in.

        Args:
            tmdb_id: TMDB's id for the person.

        Returns:
            The credits, or ``None`` if TMDB has no such person. Credits with no title
            are left out because there is nothing to show or match.
        """
        data = await self._get(f"person/{tmdb_id}/combined_credits", allow_missing=True)
        if data is None:
            return None
        wires = self._parse(_WireCast[_WireCredit], data).cast
        credits: list[PersonCredit] = []
        for wire in wires:
            production = _production(wire, wire.media_type)
            if production is not None:
                credits.append(
                    PersonCredit(
                        production=production,
                        character=_text(wire.character),
                        order=wire.order,
                    )
                )
        return credits

    async def cast(
        self, media_type: MediaType, tmdb_id: int, *, limit: int = 10
    ) -> list[CastMember] | None:
        """Return the top-billed cast of a film or series.

        Series use the aggregate credits, which cover every season.

        Args:
            media_type: Whether it is a film or a series.
            tmdb_id: TMDB's id for the production.
            limit: How many of the top-billed to return.

        Returns:
            The cast, highest billing first, or ``None`` if TMDB has no such
            production.
        """
        if media_type == "tv":
            data = await self._get(
                f"tv/{tmdb_id}/aggregate_credits", allow_missing=True
            )
            if data is None:
                return None
            tv = self._parse(_WireCast[_WireTvCast], data).cast
            members = [
                CastMember(
                    person=_person(wire),
                    character=next(
                        (c for role in wire.roles if (c := _text(role.character))),
                        None,
                    ),
                    order=wire.order,
                )
                for wire in tv
            ]
        else:
            data = await self._get(f"movie/{tmdb_id}/credits", allow_missing=True)
            if data is None:
                return None
            movie = self._parse(_WireCast[_WireMovieCast], data).cast
            members = [
                CastMember(
                    person=_person(wire),
                    character=_text(wire.character),
                    order=wire.order,
                )
                for wire in movie
            ]
        return sorted(members, key=lambda member: member.order)[: max(limit, 0)]

    def _productions(self, data: Any, media_type: MediaType) -> list[Production]:  # noqa: ANN401
        results = self._parse(_WireResults[_WireProduction], data).results
        converted = (_production(wire, media_type) for wire in results)
        return [production for production in converted if production is not None]

    async def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        *,
        allow_missing: bool = False,
    ) -> Any:  # noqa: ANN401 - untrusted JSON, validated by ``_parse``
        started = time.perf_counter()
        try:
            response = await self._client.get(path, params=params)
        except httpx2.TransportError as error:
            # Only the path is logged: the search terms and the token are never.
            logger.warning(
                "tmdb_unreachable",
                path=path,
                error=type(error).__name__,
                duration_ms=_ms(started),
            )
            msg = "Could not reach TMDB."
            raise ProviderError(msg) from error
        status = response.status_code
        missing_is_fine = status == 404 and allow_missing
        (logger.debug if status < 400 or missing_is_fine else logger.warning)(
            "tmdb_request", path=path, status=status, duration_ms=_ms(started)
        )
        if missing_is_fine:
            return None
        if response.status_code in {401, 403}:
            msg = "TMDB rejected the credentials."
            raise ProviderError(msg)
        if response.status_code == 429:
            msg = "TMDB is limiting requests."
            raise ProviderError(msg, retry_after=_retry_after(response))
        if response.status_code >= 400:
            msg = f"TMDB answered with HTTP {response.status_code}."
            raise ProviderError(msg)
        try:
            return response.json()
        except ValueError as error:
            msg = "TMDB sent something that is not JSON."
            raise ProviderError(msg) from error

    @staticmethod
    def _parse[T: BaseModel](model: type[T], data: Any) -> T:  # noqa: ANN401
        try:
            return model.model_validate(data)
        except ValidationError as error:
            msg = "TMDB changed its response format."
            raise ProviderError(msg) from error


class _WireCast[T: _Wire](_Wire):
    cast: list[T]


def _retry_after(response: httpx2.Response) -> timedelta | None:
    value = response.headers.get("Retry-After", "")
    return timedelta(seconds=int(value)) if value.isdigit() else None
