import pytest

from casttrail.providers.base import LinkTarget
from casttrail.providers.comiconomicon import parse_guest_link


@pytest.mark.parametrize(
    ("text", "key", "url"),
    [
        (
            "https://comiconomicon.com/guest/518/Andrew_Jackson",
            "518",
            "https://comiconomicon.com/guest/518/Andrew_Jackson",
        ),
        (
            "  https://comiconomicon.com/guest/518/Andrew_Jackson  ",
            "518",
            "https://comiconomicon.com/guest/518/Andrew_Jackson",
        ),
        (
            "https://www.comiconomicon.com/guest/518/Andrew_Jackson",
            "518",
            "https://comiconomicon.com/guest/518/Andrew_Jackson",
        ),
        (
            "https://COMICONOMICON.com/guest/518",
            "518",
            "https://comiconomicon.com/guest/518",
        ),
        (
            "https://comiconomicon.com/guest/518/Andrew_Jackson?ref=x#top",
            "518",
            "https://comiconomicon.com/guest/518/Andrew_Jackson",
        ),
        (
            "https://comiconomicon.com/guest/42/Jos%C3%A9_(Actor)",
            "42",
            "https://comiconomicon.com/guest/42/Jos%C3%A9_(Actor)",
        ),
    ],
)
def test_a_guest_page_address_gives_the_page(text: str, key: str, url: str) -> None:
    assert parse_guest_link(text) == LinkTarget(key=key, url=url)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "518",
        "/guest/518/Andrew_Jackson",
        # Not over https.
        "http://comiconomicon.com/guest/518/Andrew_Jackson",
        # Not Comiconomicon.
        "https://comiconomicon.com.evil.test/guest/518/Andrew_Jackson",
        "https://evilcomiconomicon.com/guest/518/Andrew_Jackson",
        "https://evil.test/comiconomicon.com/guest/518",
        "https://user@comiconomicon.com/guest/518/Andrew_Jackson",
        "https://comiconomicon.com:8443/guest/518/Andrew_Jackson",
        "https://comiconomicon.com:abc/guest/518/Andrew_Jackson",
        "https://[comiconomicon.com/guest/518/Andrew_Jackson",
        "https://www.rostercon.com/en/people/tom-wilson-en",
        # Not a guest's page.
        "https://comiconomicon.com/",
        "https://comiconomicon.com/guest/",
        "https://comiconomicon.com/event/42676/Some_Con",
        "https://comiconomicon.com/videomedia/341/Warehouse_13",
        "https://comiconomicon.com/xhr/indexautocomplete.php?term=andrew",
        # An id or name that is not plain.
        "https://comiconomicon.com/guest/abc/Andrew",
        "https://comiconomicon.com/guest/1234567890/Andrew",
        "https://comiconomicon.com/guest/518/An drew",
        "https://comiconomicon.com/guest/518/../admin",
        "https://comiconomicon.com/guest/518/" + "a" * 201,
    ],
)
def test_anything_else_is_refused(text: str) -> None:
    assert parse_guest_link(text) is None
