import pytest
from starlette.requests import Request

from casttrail.config import Settings
from casttrail.web.cookies import read_session_token, session_cookie_name

NBSP = chr(0xA0)


def _request(*cookie_headers: str, scheme: str = "http") -> Request:
    return Request(
        {
            "type": "http",
            "scheme": scheme,
            "server": ("testserver", 443 if scheme == "https" else 80),
            "path": "/",
            "query_string": b"",
            "headers": [
                (b"cookie", value.encode("latin-1")) for value in cookie_headers
            ],
        }
    )


def test_reads_the_single_matching_cookie() -> None:
    request = _request("other=1; casttrail-session=abc; more=2")

    assert read_session_token(request, Settings()) == "abc"


def test_no_cookie_gives_none() -> None:
    assert read_session_token(_request("other=1"), Settings()) is None
    assert read_session_token(_request(), Settings()) is None


def test_a_duplicated_cookie_gives_none() -> None:
    request = _request("casttrail-session=abc; casttrail-session=forged")

    assert read_session_token(request, Settings()) is None


def test_a_duplicate_split_across_header_lines_gives_none() -> None:
    request = _request("casttrail-session=abc", "casttrail-session=forged")

    assert read_session_token(request, Settings()) is None


def test_a_cookie_whose_name_only_matches_after_unicode_trimming_gives_none() -> None:
    request = _request(f"casttrail-session=abc; {NBSP}casttrail-session=forged")

    assert read_session_token(request, Settings()) is None


def test_only_the_prefixed_name_is_read_over_https() -> None:
    settings = Settings()

    over_https = _request(
        "casttrail-session=plain; __Host-Http-casttrail-session=real", scheme="https"
    )
    only_plain = _request("casttrail-session=plain", scheme="https")

    assert session_cookie_name(over_https, settings) == "__Host-Http-casttrail-session"
    assert read_session_token(over_https, settings) == "real"
    assert read_session_token(only_plain, settings) is None


@pytest.mark.parametrize(
    ("setting", "scheme", "expected"),
    [
        ("auto", "http", "casttrail-session"),
        ("auto", "https", "__Host-Http-casttrail-session"),
        ("always", "http", "__Host-Http-casttrail-session"),
        ("never", "https", "casttrail-session"),
    ],
)
def test_the_name_follows_the_secure_setting(
    setting: str, scheme: str, expected: str
) -> None:
    settings = Settings(cookie_secure=setting)

    assert session_cookie_name(_request(scheme=scheme), settings) == expected
