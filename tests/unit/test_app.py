from typing import TYPE_CHECKING

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.testclient import TestClient

from casttrail import get_version
from casttrail.config import Settings
from casttrail.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_health_reports_ok_without_disclosing_configuration(
    client: TestClient,
) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_returns_503_when_the_database_is_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: object, **_kwargs: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    monkeypatch.setattr(AsyncSession, "execute", broken)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_home_page_renders_status_and_version(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "running" in response.text
    assert get_version() in response.text


def test_home_page_says_when_the_database_is_not_migrated(
    client: TestClient,
) -> None:
    assert "not migrated" in client.get("/").text


def test_static_assets_are_served_locally(client: TestClient) -> None:
    css = client.get("/static/css/pico.min.css")
    js = client.get("/static/js/htmx.min.js")

    assert css.status_code == 200
    assert js.status_code == 200


def test_api_docs_are_disabled_by_default(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_api_docs_can_be_enabled(database_url: str) -> None:
    app = create_app(Settings(database_url=database_url, docs_enabled=True))

    with TestClient(app) as test_client:
        assert test_client.get("/docs").status_code == 200
