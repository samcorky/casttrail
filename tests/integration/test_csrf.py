from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from casttrail.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from casttrail.config import Settings

pytestmark = pytest.mark.integration

CREDENTIALS = {"email": "nobody@example.com", "password": "whatever"}


@pytest.fixture
def client(migrated_settings: Settings) -> Iterator[TestClient]:
    with TestClient(
        create_app(migrated_settings), follow_redirects=False
    ) as test_client:
        yield test_client


def test_a_cross_site_post_is_refused(client: TestClient) -> None:
    response = client.post(
        "/login", data=CREDENTIALS, headers={"Sec-Fetch-Site": "cross-site"}
    )

    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_a_cross_site_fetch_metadata_wins_over_a_matching_origin(
    client: TestClient,
) -> None:
    response = client.post(
        "/login",
        data=CREDENTIALS,
        headers={"Sec-Fetch-Site": "cross-site", "Origin": "http://testserver"},
    )

    assert response.status_code == 403


def test_a_same_origin_post_is_allowed(client: TestClient) -> None:
    response = client.post(
        "/login", data=CREDENTIALS, headers={"Sec-Fetch-Site": "same-origin"}
    )

    assert response.status_code == 401  # reached the login check, wrong credentials


@pytest.mark.parametrize("origin", ["http://testserver", "HTTP://TestServer"])
def test_a_matching_origin_is_allowed_when_fetch_metadata_is_absent(
    client: TestClient, origin: str
) -> None:
    response = client.post("/login", data=CREDENTIALS, headers={"Origin": origin})

    assert response.status_code == 401


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://testserver.evil.example",
        "https://testserver",
        "null",
        "not a url",
        "http://testserver/path",
    ],
)
def test_a_different_or_malformed_origin_is_refused(
    client: TestClient, origin: str
) -> None:
    response = client.post("/login", data=CREDENTIALS, headers={"Origin": origin})

    assert response.status_code == 403


def test_a_post_with_neither_header_is_refused(client: TestClient) -> None:
    assert client.post("/login", data=CREDENTIALS).status_code == 403


def test_logout_is_protected_too(client: TestClient) -> None:
    refused = client.post("/logout", headers={"Sec-Fetch-Site": "cross-site"})
    allowed = client.post("/logout", headers={"Sec-Fetch-Site": "same-origin"})

    assert refused.status_code == 403
    assert allowed.status_code == 303


def test_safe_methods_need_no_headers(client: TestClient) -> None:
    assert client.get("/login").status_code == 200
    assert client.get("/health").status_code == 200
