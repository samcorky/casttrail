import pytest

from casttrail.discovery.links import SOURCES, page_url_for
from casttrail.providers.comiconomicon import guest_url
from casttrail.providers.rostercon import person_url


@pytest.mark.parametrize(
    ("slug", "url"),
    [
        ("paul-mcgann-en", "https://www.rostercon.com/en/people/paul-mcgann-en"),
        ("jodie-whittaker", "https://www.rostercon.com/en/people/jodie-whittaker"),
        ("tom-wilson-2-en", "https://www.rostercon.com/en/people/tom-wilson-2-en"),
    ],
)
def test_a_rostercon_name_gives_the_persons_page(slug: str, url: str) -> None:
    assert person_url(slug) == url


@pytest.mark.parametrize(
    "slug",
    [
        "",
        " ",
        "Paul-McGann",
        "paul_mcgann",
        "paul mcgann",
        "paul/mcgann",
        "../admin",
        "paul-mcgann?x=1",
        "paul-mcgann#top",
        "-paul",
        "paul--mcgann",
        "javascript:alert(1)",
        "https://evil.test/",
        "a" * 121,
    ],
)
def test_a_rostercon_key_that_is_not_a_plain_name_gives_no_page(slug: str) -> None:
    assert person_url(slug) is None


@pytest.mark.parametrize(
    ("key", "url"),
    [
        ("518", "https://comiconomicon.com/guest/518"),
        ("1", "https://comiconomicon.com/guest/1"),
        ("123456789", "https://comiconomicon.com/guest/123456789"),
    ],
)
def test_a_comiconomicon_id_gives_the_guests_page(key: str, url: str) -> None:
    assert guest_url(key) == url


@pytest.mark.parametrize(
    "key",
    ["", "abc", "51 8", "518/x", "-518", "518.0", "1234567890", "javascript:1", "٥١٨"],
)
def test_a_comiconomicon_key_that_is_not_a_plain_number_gives_no_page(key: str) -> None:
    assert guest_url(key) is None


def test_a_page_is_found_through_its_source() -> None:
    assert page_url_for("rostercon", "paul-mcgann-en") == (
        "https://www.rostercon.com/en/people/paul-mcgann-en"
    )
    assert page_url_for("comiconomicon", "518") == "https://comiconomicon.com/guest/518"


def test_a_key_is_only_ever_read_by_its_own_source() -> None:
    # A number is a valid Rostercon name, but a name is not a Comiconomicon id.
    assert page_url_for("comiconomicon", "paul-mcgann-en") is None
    assert page_url_for("rostercon", "518") == "https://www.rostercon.com/en/people/518"


@pytest.mark.parametrize("provider", ["tmdb", "imdb", "wikipedia", ""])
def test_a_source_we_do_not_link_to_gives_no_page(provider: str) -> None:
    assert page_url_for(provider, "518") is None


def test_every_source_can_say_where_its_pages_are() -> None:
    assert [s.provider for s in SOURCES if s.page_url("518") is not None] == [
        "rostercon",
        "comiconomicon",
    ]
