import struct
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


def test_status_page_renders_status_and_version(client: TestClient) -> None:
    response = client.get("/status")

    assert response.status_code == 200
    assert "running" in response.text
    assert get_version() in response.text


def test_status_page_says_when_the_database_is_not_migrated(
    settings: Settings,
) -> None:
    off = settings.model_copy(update={"auto_migrate": False})

    with TestClient(create_app(off)) as unmigrated:
        assert "not migrated" in unmigrated.get("/status").text


def test_a_fresh_database_is_migrated_on_start(client: TestClient) -> None:
    assert "not migrated" not in client.get("/status").text
    assert client.get("/health").json() == {"status": "ok"}


def test_static_assets_are_served_locally(client: TestClient) -> None:
    for path in (
        "css/bootstrap.min.css",
        "js/bootstrap.bundle.min.js",
        "js/htmx.min.js",
        "js/theme.js",
        "css/app.css",
    ):
        assert client.get(f"/static/{path}").status_code == 200, path


def test_the_old_framework_is_gone(client: TestClient) -> None:
    assert client.get("/static/css/pico.min.css").status_code == 404
    assert client.get("/static/js/nav.js").status_code == 404


def test_api_docs_are_disabled_by_default(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_api_docs_can_be_enabled(database_url: str) -> None:
    app = create_app(Settings(database_url=database_url, docs_enabled=True))

    with TestClient(app) as test_client:
        assert test_client.get("/docs").status_code == 200


def _png_size(data: bytes) -> tuple[int, int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, _depth, colour_type = struct.unpack(">IIBB", data[16:26])
    return width, height, colour_type


@pytest.mark.parametrize(
    ("name", "size"),
    [
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("icon-maskable-512.png", 512),
        ("apple-touch-icon.png", 180),
        ("favicon-32.png", 32),
    ],
)
def test_app_icons_are_served_at_their_declared_size(
    client: TestClient, name: str, size: int
) -> None:
    response = client.get(f"/static/img/{name}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert _png_size(response.content)[:2] == (size, size)


def test_the_maskable_icon_is_full_bleed_and_the_others_have_transparent_corners(
    client: TestClient,
) -> None:
    rgb, rgba = 2, 6

    maskable = client.get("/static/img/icon-maskable-512.png").content
    rounded = client.get("/static/img/icon-512.png").content

    assert _png_size(maskable)[2] == rgb
    assert _png_size(rounded)[2] == rgba


def test_the_logo_is_an_svg(client: TestClient) -> None:
    for name in ("logo.svg", "logo-maskable.svg"):
        response = client.get(f"/static/img/{name}")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/svg+xml")
        assert "<svg" in response.text


def test_pages_declare_the_favicon_and_touch_icon(client: TestClient) -> None:
    page = client.get("/login").text

    assert 'rel="icon" type="image/svg+xml"' in page
    assert "/static/img/favicon-32.png" in page
    assert 'rel="apple-touch-icon"' in page


def test_the_sign_in_page_shows_the_logo(client: TestClient) -> None:
    assert 'alt="CastTrail"' in client.get("/login").text


def test_the_default_favicon_path_redirects_to_the_icon(client: TestClient) -> None:
    redirect = client.get("/favicon.ico", follow_redirects=False)
    followed = client.get("/favicon.ico")

    assert redirect.status_code == 308
    assert redirect.headers["location"] == "/static/img/favicon-32.png"
    assert followed.status_code == 200
    assert followed.headers["content-type"] == "image/png"
