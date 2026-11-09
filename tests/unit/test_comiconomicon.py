from datetime import date
from pathlib import Path

import pytest

from casttrail.providers.comiconomicon import (
    parse_dates,
    parse_event_list,
    parse_event_page,
    parse_guest_page,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "comiconomicon"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _event_block(
    *,
    event_id: str = "1",
    name: str = "Some Con",
    href: str | None = "/event/1/Some_Con",
    when: str = "17 - 18 Apr, 2027",
    where: str = "A Hotel Lincoln United Kingdom",
) -> str:
    href_attr = f' href="{href}"' if href is not None else ""
    return "".join(
        [
            '<div class="searchresult eventsearchresult  ">',
            '<a class="eventname clickredirect" data-type="event" ',
            f'data-id="{event_id}" data-name="{name}"{href_attr}>{name}</a>',
            f'<div class="eventlinks"><div class="eventdate">{when}</div>',
            f'<div class="eventlocation">{where}</div></div></div>',
        ]
    )


# --- dates ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("17 - 18 Apr, 2027", (date(2027, 4, 17), date(2027, 4, 18))),
        ("13 Nov, 2026", (date(2026, 11, 13), date(2026, 11, 13))),
        ("30 Apr - 2 May, 2027", (date(2027, 4, 30), date(2027, 5, 2))),
        ("30 Dec - 2 Jan, 2027", (date(2026, 12, 30), date(2027, 1, 2))),
        ("2 - 6 Sep, 2027", (date(2027, 9, 2), date(2027, 9, 6))),
        ("29 Feb, 2028", (date(2028, 2, 29), date(2028, 2, 29))),
    ],
)
def test_dates_are_read(text: str, expected: tuple[date, date]) -> None:
    assert parse_dates(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "TBC",
        "17 Apr",
        "17 - 18 Foo, 2027",
        "17 Foo - 18 Apr, 2027",
        "31 Apr, 2027",
        "29 Feb, 2027",
        "1 - 2 - 3 Apr, 2027",
        "17 Apr, 2027 - 18",
        "Apr 17, 2027",
    ],
)
def test_dates_that_cannot_be_read_are_unknown_not_guessed(text: str) -> None:
    assert parse_dates(text) == (None, None)


# --- an event page ----------------------------------------------------------------


def test_a_real_event_page_gives_the_convention_and_its_guests() -> None:
    parsed = parse_event_page(
        _fixture("event_42676_follow_the_yellow_gate_road.html"), etag='"e"'
    )

    convention = parsed.convention
    assert convention is not None
    assert (convention.source_key, convention.name) == (
        "42676",
        "Follow The Yellow Gate Road (Take 4)",
    )
    assert convention.source_url == (
        "https://comiconomicon.com/event/42676/Follow_The_Yellow_Gate_Road_Take_4_"
    )
    assert (convention.start_date, convention.end_date) == (
        date(2027, 4, 17),
        date(2027, 4, 18),
    )
    assert convention.venue is not None
    assert "Lincoln" in convention.venue
    assert convention.country == "united-kingdom"
    assert (convention.city, convention.organizer, convention.status) == (
        None,
        None,
        None,
    )
    assert [(g.source_key, g.name) for g in parsed.guests.items] == [
        ("507", "Alexis Cruz"),
        ("518", "Andrew Jackson"),
        ("37296", "David Read"),
    ]
    assert parsed.guests.items[1].source_url == (
        "https://comiconomicon.com/guest/518/Andrew_Jackson"
    )
    assert parsed.guests.etag == '"e"'


def test_the_guest_list_is_never_complete_so_nobody_is_unlisted_by_absence() -> None:
    parsed = parse_event_page(_fixture("event_42682_london_film_comic_con.html"))

    assert len(parsed.guests.items) == 18
    assert parsed.guests.complete is False


def test_an_england_ending_is_the_uk_with_a_region() -> None:
    convention = parse_event_page(
        _fixture("event_42682_london_film_comic_con.html")
    ).convention

    assert convention is not None
    assert convention.name == "London Film & Comic Con"
    assert (convention.country, convention.region) == ("united-kingdom", "England")
    assert convention.venue == "Olympia, London, England"


@pytest.mark.parametrize(
    ("where", "country", "region"),
    [
        ("Hall, Cardiff, Wales", "united-kingdom", "Wales"),
        ("Hall Glasgow Scotland", "united-kingdom", "Scotland"),
        ("Hall, Belfast, Northern Ireland", "united-kingdom", "Northern Ireland"),
        ("Hall Lincoln United Kingdom", "united-kingdom", None),
        ("Hall, Lincoln, United Kingdom.", "united-kingdom", None),
        ("Suburban Collection Showplace Novi, MI", None, None),
        ("Paris, France", None, None),
        ("New England Expo Boston MA", None, None),
        ("Hall, Newengland", None, None),
        ("", None, None),
    ],
)
def test_only_a_uk_name_at_the_end_makes_it_a_uk_event(
    where: str, country: str | None, region: str | None
) -> None:
    html = f"<main>{_event_block(where=where)}</main>"

    convention = parse_event_page(html).convention

    assert convention is not None
    assert (convention.country, convention.region) == (country, region)


def test_a_page_without_an_event_is_no_convention_and_an_empty_incomplete_list() -> (
    None
):
    parsed = parse_event_page("<html><body>new design</body></html>", etag="x")

    assert parsed.convention is None
    assert (parsed.guests.items, parsed.guests.complete, parsed.guests.etag) == (
        [],
        False,
        "x",
    )


@pytest.mark.parametrize("html", ["", "<", "<div class=", "\x00" * 50])
def test_garbage_never_raises(html: str) -> None:
    assert parse_event_page(html).convention is None
    assert parse_event_list(html).events == []
    assert parse_guest_page(html).roles == []


def test_an_unreadable_date_leaves_the_event_with_unknown_dates() -> None:
    html = f"<main>{_event_block(when='TBC')}</main>"

    convention = parse_event_page(html).convention

    assert convention is not None
    assert (convention.start_date, convention.end_date) == (None, None)


def _guest(id_: str, name: str, href: str | None) -> str:
    link = f' href="{href}"' if href else ""
    return f'<a class="eventguest" data-id="{id_}" data-name="{name}"{link}>x</a>'


def test_a_guest_with_a_bad_link_or_no_name_is_left_out() -> None:
    html = "".join(
        [
            f"<main>{_event_block()}",
            _guest("1", "Good Guest", "/guest/1/Good"),
            _guest("2", "Evil", "https://evil.test/guest/2"),
            _guest("3", "Wrong Id", "/guest/4/Wrong"),
            _guest("5", "  ", "/guest/5/Blank"),
            _guest("6", "No Link", None),
            _guest("7", "Dup", "/guest/7/A"),
            _guest("7", "Dup Again", "/guest/7/B"),
            "</main>",
        ]
    )

    guests = parse_event_page(html).guests.items

    assert [(g.source_key, g.name) for g in guests] == [
        ("1", "Good Guest"),
        ("7", "Dup"),
    ]


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.test/event/1/x",
        "//evil.test/event/1/x",
        "/event/1/../../etc",
        "/event/1/x?next=http://evil.test",
        "/event/1/x#frag",
        "/event/abc/x",
        "/event/1/x y",
        "javascript:alert(1)",
        "/guest/1/x",
        "/event/1234567890/x",
        None,
    ],
)
def test_an_event_link_that_is_not_our_own_site_is_refused(href: str | None) -> None:
    html = f"<main>{_event_block(href=href)}</main>"

    parsed = parse_event_page(html)

    assert parsed.convention is None
    assert parse_event_list(html).skipped == 1


def test_an_event_whose_data_id_disagrees_with_its_link_is_refused() -> None:
    html = f"<main>{_event_block(event_id='2', href='/event/1/Some_Con')}</main>"

    assert parse_event_page(html).convention is None


def test_markup_in_names_is_text_not_markup() -> None:
    html = f"<main>{_event_block(name='A &lt;b&gt;Bold&lt;/b&gt; &amp; Co')}</main>"

    convention = parse_event_page(html).convention

    assert convention is not None
    assert convention.name == "A <b>Bold</b> & Co"


# --- event lists (film, series and guest pages) -----------------------------------


def test_a_real_film_page_lists_its_events() -> None:
    listing = parse_event_list(_fixture("videomedia_341_warehouse_13.html"))

    assert listing.skipped == 0
    assert len(listing.events) > 10
    first = listing.events[0]
    assert (first.source_key, first.name) == ("43583", "Motor City Comic Con")
    assert (first.start_date, first.end_date) == (
        date(2026, 11, 13),
        date(2026, 11, 15),
    )
    assert first.location == "Suburban Collection Showplace Novi, MI"
    assert (
        first.source_url == "https://comiconomicon.com/event/43583/Motor_City_Comic_Con"
    )
    keys = [e.source_key for e in listing.events]
    assert len(keys) == len(set(keys))


def test_a_multi_line_location_is_one_line() -> None:
    listing = parse_event_list(_fixture("videomedia_341_warehouse_13.html"))

    second = next(e for e in listing.events if e.source_key == "42991")
    assert (
        second.location
        == "Suburban Collection Showplace Grand River Avenue Novi Michigan USA"
    )


def test_the_new_appearances_marker_does_not_disturb_an_event() -> None:
    listing = parse_event_list(_fixture("videomedia_341_warehouse_13.html"))

    last = listing.events[-1]
    assert (last.source_key, last.name) == ("43925", "The Blood Legacy 4")
    assert last.start_date == date(2027, 10, 23)


def test_a_repeated_event_is_listed_once() -> None:
    html = f"<main>{_event_block()}{_event_block(name='Renamed')}</main>"

    events = parse_event_list(html).events

    assert [e.name for e in events] == ["Some Con"]


# --- a guest page -----------------------------------------------------------------


def test_a_real_guest_page_gives_their_events_and_roles() -> None:
    page = parse_guest_page(_fixture("guest_518_andrew_jackson.html"))

    assert page.skipped == 0
    assert [(e.source_key, e.name) for e in page.events] == [
        ("42676", "Follow The Yellow Gate Road (Take 4)")
    ]
    assert page.events[0].start_date == date(2027, 4, 17)
    assert [(r.source_key, r.title, r.role, r.detail) for r in page.roles] == [
        ("40956", "Scott Pilgrim Takes Off", "Lobby Announcer", "(2023)"),
        ("41865", "Catch Me If You Claus", "Santa", "(TV Movie 2023)"),
        ("33250", "Star Trek: Strange New Worlds", "Captain D'Chok", "(2022)"),
        ("22713", "The Boys", "Love Sausage", "(TV Series 2019)"),
    ]
    assert (
        page.roles[3].source_url
        == "https://comiconomicon.com/videomedia/22713/The_Boys"
    )


def _role(role: str, id_: str, href: str, title: str) -> str:
    link = f'<a class="rolevideomedianame" data-id="{id_}" href="{href}">{title}</a>'
    return f'<div class="role">{role}</div>{link}'


def test_a_role_with_a_bad_link_is_left_out() -> None:
    html = "".join(
        [
            _role("Hero", "1", "https://evil.test/videomedia/1/x", "Evil"),
            _role("Villain", "2", "/videomedia/2/Good", "Good"),
            _role("Extra", "9", "/videomedia/3/Mismatch", "Bad"),
        ]
    )

    roles = parse_guest_page(html).roles

    assert [(r.source_key, r.title, r.role, r.detail) for r in roles] == [
        ("2", "Good", "Villain", None)
    ]


def test_a_film_listed_twice_on_a_guest_page_keeps_the_first() -> None:
    html = "".join(
        [
            _role("Hero", "2", "/videomedia/2/Good", "Good"),
            _role("Villain", "2", "/videomedia/2/Good", "Good Again"),
        ]
    )

    roles = parse_guest_page(html).roles

    assert [(r.title, r.role) for r in roles] == [("Good", "Hero")]
