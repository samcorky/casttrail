from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from casttrail import get_version
from casttrail.config import Settings
from casttrail.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(Settings())) as test_client:
        yield test_client


def test_health_reports_ok_without_disclosing_configuration(
    client: TestClient,
) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_home_page_renders_status_and_version(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "running" in response.text
    assert get_version() in response.text


def test_static_assets_are_served_locally(client: TestClient) -> None:
    css = client.get("/static/css/pico.min.css")
    js = client.get("/static/js/htmx.min.js")

    assert css.status_code == 200
    assert js.status_code == 200


def test_api_docs_are_disabled_by_default(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_api_docs_can_be_enabled() -> None:
    app = create_app(Settings(docs_enabled=True))

    with TestClient(app) as test_client:
        assert test_client.get("/docs").status_code == 200
