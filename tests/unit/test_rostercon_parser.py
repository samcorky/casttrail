import json
from datetime import date
from pathlib import Path

import pytest

from casttrail.providers.rostercon import parse_event_page

FIXTURES = Path(__file__).parents[1] / "fixtures" / "rostercon"
EVENT_URL = "https://www.rostercon.com/en/event-convention/some-event"
PERSON_URL = "https://www.rostercon.com/en/people/some-guest"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _page(event: dict[str, object] | None, *, extra: str = "") -> str:
    blocks = (
        []
        if event is None
        else [
            json.dumps(
                {
                    "@context": "https://schema.org",
                    "@graph": [{"@type": "WebPage"}, event],
                }
            )
        ]
    )
    scripts = "".join(
        f'<script type="application/ld+json">{b}</script>' for b in blocks
    )
    return f"<!doctype html><title>x</title>{scripts}{extra}"


def _event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "@type": "Event",
        "name": "Some Event",
        "url": EVENT_URL,
        "performer": [{"@type": "Person", "name": "A Guest", "url": PERSON_URL}],
    }
    return event | overrides


# --- the real pages -------------------------------------------------------------


def test_a_real_event_page_gives_its_convention() -> None:
    parsed = parse_event_page(_fixture("event_follow-the-yellow-gate-road-take-4.html"))

    convention = parsed.convention
    assert convention is not None
    assert convention.source_key == "follow-the-yellow-gate-road-take-4"
    assert convention.name == "Follow The Yellow Gate Road (Take 4)"
    assert convention.source_url == (
        "https://www.rostercon.com/en/event-convention/follow-the-yellow-gate-road-take-4"
    )
    assert (convention.start_date, convention.end_date) == (
        date(2027, 4, 17),
        date(2027, 4, 18),
    )
    assert convention.city == "Lincoln"
    assert convention.region == "England"
    assert convention.country == "united-kingdom"
    assert convention.venue is not None
    assert convention.venue.startswith("Best Western Plus Bentley Hotel")
    assert convention.organizer == "Mama Nox Presents"
    assert convention.status == "scheduled"


def test_a_real_event_page_gives_its_guests_with_stable_keys() -> None:
    parsed = parse_event_page(_fixture("event_follow-the-yellow-gate-road-take-4.html"))

    assert parsed.guests.complete is True
    assert [(g.source_key, g.name) for g in parsed.guests.items] == [
        ("alexis-cruz-en", "Alexis Cruz"),
        ("andrew-jackson-en", "Andrew Jackson"),
        ("michael-shanks-en", "Michael Shanks"),
    ]
    first = parsed.guests.items[0]
    assert first.source_url == "https://www.rostercon.com/en/people/alexis-cruz-en"
    assert first.identifiers == {}
    assert first.status is None


def test_a_bigger_real_page_has_every_guest_once() -> None:
    parsed = parse_event_page(_fixture("event_festival-of-the-timelords-2026.html"))

    keys = [g.source_key for g in parsed.guests.items]
    assert len(keys) == 15
    assert len(set(keys)) == 15
    assert "paul-mcgann-en" in keys
    assert "jo-martin" in keys  # slugs do not always end in "-en"
    assert parsed.guests.complete is True


def test_a_cancelled_event_says_so() -> None:
    parsed = parse_event_page(_fixture("event_star-con-2026_cancelled.html"))

    assert parsed.convention is not None
    assert parsed.convention.status == "cancelled"
    assert parsed.convention.city == "Hull"
    assert [g.name for g in parsed.guests.items] == ["Clem So", "Paul Kasey"]


def test_the_country_comes_from_the_url_not_the_french_address() -> None:
    parsed = parse_event_page(_fixture("event_star-con-2026_cancelled.html"))

    assert parsed.convention is not None
    assert "Royaume" not in (parsed.convention.country or "")
    assert parsed.convention.country == "united-kingdom"


def test_the_etag_is_passed_through() -> None:
    parsed = parse_event_page(
        _fixture("event_star-con-2026_cancelled.html"), etag='"abc123"'
    )

    assert parsed.guests.etag == '"abc123"'


# --- status ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://schema.org/EventScheduled", "scheduled"),
        ("https://schema.org/EventCancelled", "cancelled"),
        ("https://schema.org/EventPostponed", "postponed"),
        ("https://schema.org/EventRescheduled", "postponed"),
        ("https://schema.org/EventMovedOnline", None),
        ("nonsense", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_only_known_statuses_are_understood(raw: object, expected: str | None) -> None:
    parsed = parse_event_page(_page(_event(eventStatus=raw)))

    assert parsed.convention is not None
    assert parsed.convention.status == expected


def test_a_missing_status_is_unknown_not_cancelled() -> None:
    parsed = parse_event_page(_page(_event()))

    assert parsed.convention is not None
    assert parsed.convention.status is None


# --- the guest list is only trusted when it is whole ----------------------------


def test_an_event_with_no_performer_list_is_not_a_complete_listing() -> None:
    event = _event()
    del event["performer"]

    parsed = parse_event_page(_page(event))

    assert parsed.convention is not None
    assert parsed.guests.items == []
    assert parsed.guests.complete is False


def test_an_empty_performer_list_is_a_complete_empty_listing() -> None:
    parsed = parse_event_page(_page(_event(performer=[])))

    assert parsed.guests.items == []
    assert parsed.guests.complete is True


def test_one_bad_guest_is_skipped_and_the_listing_is_marked_incomplete() -> None:
    performers = [
        {"@type": "Person", "name": "Good Guest", "url": PERSON_URL},
        {"@type": "Person", "name": "No Link"},
        {"@type": "Person", "url": PERSON_URL + "-2"},
        "not even an object",
    ]

    parsed = parse_event_page(_page(_event(performer=performers)))

    assert [g.name for g in parsed.guests.items] == ["Good Guest"]
    assert parsed.guests.complete is False


def test_a_single_performer_object_is_accepted() -> None:
    performer = {"@type": "Person", "name": "Only One", "url": PERSON_URL}

    parsed = parse_event_page(_page(_event(performer=performer)))

    assert [g.name for g in parsed.guests.items] == ["Only One"]
    assert parsed.guests.complete is True


@pytest.mark.parametrize("performers", ["a string", 5, True])
def test_a_performer_field_of_the_wrong_type_is_incomplete(performers: object) -> None:
    parsed = parse_event_page(_page(_event(performer=performers)))

    assert parsed.guests.items == []
    assert parsed.guests.complete is False


def test_a_guest_listed_twice_is_kept_once() -> None:
    performers = [
        {"@type": "Person", "name": "Twice", "url": PERSON_URL},
        {"@type": "Person", "name": "Twice Again", "url": PERSON_URL},
    ]

    parsed = parse_event_page(_page(_event(performer=performers)))

    assert [g.name for g in parsed.guests.items] == ["Twice"]
    assert parsed.guests.complete is True


# --- addresses from outside are not trusted -------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://www.rostercon.com/en/people/x",
        "https://evil.example/en/people/x",
        "https://www.rostercon.com.evil.example/en/people/x",
        "https://www.rostercon.com/en/event-convention/x",
        "https://www.rostercon.com/en/people/",
        "https://www.rostercon.com/en/people/a/b",
        "javascript:alert(1)",
        "//www.rostercon.com/en/people/x",
        "",
    ],
)
def test_a_guest_link_off_rostercon_is_dropped(url: str) -> None:
    performers = [{"@type": "Person", "name": "Odd", "url": url}]

    parsed = parse_event_page(_page(_event(performer=performers)))

    assert parsed.guests.items == []
    assert parsed.guests.complete is False


@pytest.mark.parametrize(
    "url",
    [
        "http://www.rostercon.com/en/event-convention/x",
        "https://evil.example/en/event-convention/x",
        "https://www.rostercon.com/en/people/x",
        "https://www.rostercon.com/en/event-convention/",
        None,
    ],
)
def test_an_event_that_is_not_on_rostercon_is_no_convention(url: object) -> None:
    parsed = parse_event_page(_page(_event(url=url)))

    assert parsed.convention is None
    assert parsed.guests.complete is False
    assert parsed.guests.items == []


# --- pages that have changed shape never raise ----------------------------------


@pytest.mark.parametrize(
    "html",
    [
        "",
        "   ",
        "<html><body>nothing here</body></html>",
        _page(None),
        '<script type="application/ld+json">{not json</script>',
        '<script type="application/ld+json">[]</script>',
        '<script type="application/ld+json">"just a string"</script>',
        '<script type="application/ld+json">{"@type": "Person", "name": "X"}</script>',
        "<script type='application/ld+json'>",
    ],
)
def test_a_page_with_no_usable_event_is_no_convention_and_incomplete(html: str) -> None:
    parsed = parse_event_page(html)

    assert parsed.convention is None
    assert parsed.guests.items == []
    assert parsed.guests.complete is False


@pytest.mark.parametrize(
    "location",
    [
        {"url": "https://www.rostercon.com/en/somewhere/else"},
        {"url": "https://www.rostercon.com/en/location/"},
        {"url": 5},
        {"name": "No url at all"},
        "not an object",
    ],
)
def test_a_location_without_a_usable_country_gives_none(location: object) -> None:
    parsed = parse_event_page(_page(_event(location=location)))

    assert parsed.convention is not None
    assert parsed.convention.country is None


def test_a_nameless_event_is_no_convention() -> None:
    parsed = parse_event_page(_page(_event(name="   ")))

    assert parsed.convention is None


def test_missing_optional_details_are_none() -> None:
    parsed = parse_event_page(_page(_event()))

    convention = parsed.convention
    assert convention is not None
    assert (convention.start_date, convention.end_date) == (None, None)
    assert (convention.venue, convention.city, convention.region) == (None, None, None)
    assert (convention.country, convention.organizer) == (None, None)


@pytest.mark.parametrize("value", ["", "soon", "2026-13-45", 20260101, None])
def test_an_unreadable_date_is_none(value: object) -> None:
    parsed = parse_event_page(_page(_event(startDate=value)))

    assert parsed.convention is not None
    assert parsed.convention.start_date is None


def test_an_event_found_among_other_blocks_and_a_list_type() -> None:
    other = '<script type="application/ld+json">{"@type": "WebSite"}</script>'
    event = json.dumps(_event(**{"@type": ["Thing", "Event"]}))
    html = f'{other}<script type="application/ld+json">[{event}]</script>'

    parsed = parse_event_page(html)

    assert parsed.convention is not None
    assert parsed.convention.name == "Some Event"


def test_page_text_that_merely_looks_like_json_ld_is_ignored() -> None:
    html = f"<p>{json.dumps(_event())}</p><pre>application/ld+json</pre>"

    assert parse_event_page(html).convention is None


def test_names_with_odd_characters_survive() -> None:
    performers = [
        {"@type": "Person", "name": "Zoë Ball & <Co>", "url": PERSON_URL},
    ]

    parsed = parse_event_page(
        _page(_event(name="Con & Café “2027”", performer=performers))
    )

    assert parsed.convention is not None
    assert parsed.convention.name == "Con & Café “2027”"
    assert parsed.guests.items[0].name == "Zoë Ball & <Co>"
