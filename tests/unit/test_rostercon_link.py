import pytest

from casttrail.providers.rostercon import PersonPage, parse_person_link


@pytest.mark.parametrize(
    "text",
    [
        "https://www.rostercon.com/en/people/tom-wilson-en",
        "  https://www.rostercon.com/en/people/tom-wilson-en  ",
        "https://www.rostercon.com/en/people/tom-wilson-en/",
        "https://rostercon.com/en/people/tom-wilson-en",
        "https://WWW.RosterCon.com/en/people/tom-wilson-en",
        "https://www.rostercon.com/en/people/tom-wilson-en?utm_source=x#top",
    ],
)
def test_a_person_page_address_gives_the_page(text: str) -> None:
    assert parse_person_link(text) == PersonPage(
        slug="tom-wilson-en", url="https://www.rostercon.com/en/people/tom-wilson-en"
    )


def test_a_page_without_the_language_suffix_is_fine() -> None:
    page = parse_person_link("https://www.rostercon.com/en/people/jodie-whittaker")

    assert page == PersonPage(
        slug="jodie-whittaker",
        url="https://www.rostercon.com/en/people/jodie-whittaker",
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "tom-wilson-en",
        "/en/people/tom-wilson-en",
        # Not over https.
        "http://www.rostercon.com/en/people/tom-wilson-en",
        # Not Rostercon.
        "https://www.rostercon.com.evil.test/en/people/tom-wilson-en",
        "https://evil.test/www.rostercon.com/en/people/tom-wilson-en",
        "https://evilrostercon.com/en/people/tom-wilson-en",
        "https://user@www.rostercon.com/en/people/tom-wilson-en",
        "https://www.rostercon.com:8443/en/people/tom-wilson-en",
        "https://www.rostercon.com:abc/en/people/tom-wilson-en",
        "https://[www.rostercon.com/en/people/tom-wilson-en",
        "file:///etc/passwd",
        # Not a person's page.
        "https://www.rostercon.com/",
        "https://www.rostercon.com/en/people/",
        "https://www.rostercon.com/en/event-convention/pensacon-2027",
        "https://www.rostercon.com/en/people/tom-wilson-en/events/123",
        "https://www.rostercon.com/fr/people/tom-wilson-en",
        # A slug that is not plain words.
        "https://www.rostercon.com/en/people/Tom_Wilson",
        "https://www.rostercon.com/en/people/tom%2Fwilson",
        "https://www.rostercon.com/en/people/../admin",
        "https://www.rostercon.com/en/people/-tom",
        "https://www.rostercon.com/en/people/tom--wilson",
        "https://www.rostercon.com/en/people/" + "a" * 121,
    ],
)
def test_anything_else_is_refused(text: str) -> None:
    assert parse_person_link(text) is None
