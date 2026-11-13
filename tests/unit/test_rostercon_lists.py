from datetime import date
from pathlib import Path

import pytest

from casttrail.providers.rostercon import (
    parse_all_events_link,
    parse_event_rows,
    slugify_name,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "rostercon"
EVENT = "https://www.rostercon.com/en/event-convention/"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _row(
    *,
    when: str | None = "Jul 16th 2027",
    name: str = "A Con",
    href: str | None = EVENT + "a-con",
    place: str | None = "Lincoln",
    show: str | None = "Doctor Who",
) -> str:
    parts = ['<ul class="list-inline row">']
    if when is not None:
        parts.append(f'<li class="date-list-event col-6"><span>x</span> {when}</li>')
    link = (
        f'<a href="{href}" rel="bookmark"><strong>{name}</strong></a>' if href else name
    )
    parts.append(f'<li class="col-12 name-list-event order-1">{link}</li>')
    if place is not None:
        parts.append(f'<li class="location-list-event"><a href="/x">{place}</a></li>')
    if show is not None:
        parts.append(f'<li class="serie-list-event"><a href="/y">{show}</a></li>')
    parts.append("</ul>")
    return "".join(parts)


# --- the real person page and event lists ---------------------------------------


def test_a_real_person_page_gives_its_events_in_order() -> None:
    rows = parse_event_rows(_fixture("person_paul-mcgann-en_page.html"))

    assert rows.skipped == 0
    assert [(e.start_date, e.source_key) for e in rows.events] == [
        (date(2026, 10, 24), "fan-tc-con-2026"),
        (date(2026, 6, 13), "em-con-nottingham-2026"),
        (date(2026, 5, 30), "festival-of-the-timelords-2026"),
        (date(2026, 5, 9), "the-capitol-fifty-years-of-dwas-2026"),
        (date(2027, 2, 13), "for-the-love-of-time-space-2027"),
        (date(2027, 2, 19), "pensacon-2027"),
    ]


def test_a_real_row_has_a_name_a_place_and_a_show() -> None:
    events = parse_event_rows(_fixture("person_paul-mcgann-en_page.html")).events

    pensacon = events[-1]
    assert pensacon.name == "Pensacon 2027"
    assert pensacon.source_url == EVENT + "pensacon-2027"
    assert pensacon.place == "Pensacola (FL)"
    assert pensacon.show == "Multi-Fandom"


def test_the_full_events_page_lists_more_than_the_teaser() -> None:
    teaser = parse_event_rows(_fixture("person_paul-mcgann-en_page.html")).events
    full = parse_event_rows(_fixture("person_paul-mcgann-en_events_page1.html"))

    assert full.skipped == 0
    assert len(full.events) == 14
    assert {e.source_key for e in teaser} <= {e.source_key for e in full.events}
    assert [e.start_date for e in full.events] == sorted(
        (e.start_date for e in full.events if e.start_date), reverse=True
    )


@pytest.mark.parametrize("page", ["list_uk_page1.html", "list_uk_page2.html"])
def test_the_country_list_pages_parse_the_same_way(page: str) -> None:
    rows = parse_event_rows(_fixture(page))

    assert rows.skipped == 0
    assert len(rows.events) in (14, 15)
    assert all(e.start_date and e.source_url.startswith(EVENT) for e in rows.events)
    assert all(e.place for e in rows.events)


def test_the_country_list_is_only_roughly_in_date_order() -> None:
    """Its first row is out of place, so a sweep cannot stop at the first past date."""
    dates = [
        e.start_date
        for e in parse_event_rows(_fixture("list_uk_page1.html")).events
        if e.start_date
    ]

    assert dates != sorted(dates, reverse=True)
    assert dates[1:] == sorted(dates[1:], reverse=True)


def test_the_real_page_links_to_its_full_events_list() -> None:
    link = parse_all_events_link(_fixture("person_paul-mcgann-en_page.html"))

    assert link == "https://www.rostercon.com/en/people/paul-mcgann-en/events/18489"


# --- rows that are odd ----------------------------------------------------------


def test_a_row_with_no_place_or_show_still_counts() -> None:
    rows = parse_event_rows(_row(place=None, show=None))

    assert rows.skipped == 0
    assert (rows.events[0].place, rows.events[0].show) == (None, None)


def test_a_row_with_no_event_link_is_skipped_and_counted() -> None:
    html = _row(href=None) + _row(name="Good", href=EVENT + "good")

    rows = parse_event_rows(html)

    assert [e.name for e in rows.events] == ["Good"]
    assert rows.skipped == 1


@pytest.mark.parametrize(
    "href",
    [
        "http://www.rostercon.com/en/event-convention/x",
        "https://evil.example/en/event-convention/x",
        "https://www.rostercon.com/en/people/x",
        "https://www.rostercon.com/en/event-convention/",
        "https://www.rostercon.com/en/event-convention/a/b",
        "javascript:alert(1)",
    ],
)
def test_a_row_pointing_off_rostercon_is_skipped(href: str) -> None:
    rows = parse_event_rows(_row(href=href))

    assert rows.events == []
    assert rows.skipped == 1


def test_a_row_with_an_empty_name_is_skipped() -> None:
    assert parse_event_rows(_row(name="   ")).skipped == 1


@pytest.mark.parametrize("when", ["x", "soon", "Feb 30th 2027", "Zzz 1st 2027"])
def test_an_unreadable_date_is_none_but_the_row_survives(when: str) -> None:
    rows = parse_event_rows(_row(when=when))

    assert len(rows.events) == 1
    assert rows.events[0].start_date is None


def test_a_row_with_no_date_cell_at_all_is_not_a_row() -> None:
    rows = parse_event_rows(_row(when=None))

    assert (rows.events, rows.skipped) == ([], 0)


def test_other_lists_on_the_page_are_ignored() -> None:
    html = "".join(
        [
            '<ul class="menu"><li class="date-list-event">Jan 1st 2027</li></ul>',
            '<ul class="list-inline"><li>x</li></ul>',
            _row(),
        ]
    )

    assert len(parse_event_rows(html).events) == 1


@pytest.mark.parametrize(
    "html", ["", "   ", "<html></html>", "<ul class='list-inline row'>"]
)
def test_a_page_with_no_rows_gives_nothing_and_never_raises(html: str) -> None:
    rows = parse_event_rows(html)

    assert (rows.events, rows.skipped) == ([], 0)


def test_names_with_entities_and_odd_characters_survive() -> None:
    rows = parse_event_rows(_row(name="Con &amp; Café &ndash; &quot;2027&quot;"))

    assert rows.events[0].name == 'Con & Café – "2027"'  # noqa: RUF001 - the en dash is the point


def test_whitespace_in_a_name_is_collapsed() -> None:
    row = _row(name="The Land Con\n  in Scotland\n2")

    assert parse_event_rows(row).events[0].name == "The Land Con in Scotland 2"


# --- the link to all events -----------------------------------------------------


@pytest.mark.parametrize(
    "html",
    [
        "",
        '<a href="/en/people/x/events/1">relative</a>',
        '<a href="http://www.rostercon.com/en/people/x/events/1">http</a>',
        '<a href="https://evil.example/en/people/x/events/1">elsewhere</a>',
        '<a href="https://www.rostercon.com/en/people/x/events/abc">bad id</a>',
        '<a href="https://www.rostercon.com/en/people/x/events/1?pagination_rc=2">p</a>',
        '<a href="https://www.rostercon.com/en/people/x/y/events/1">deep</a>',
        '<a href="https://www.rostercon.com/en/tv-show/x/events/1">wrong kind</a>',
    ],
)
def test_only_a_proper_events_link_on_rostercon_is_returned(html: str) -> None:
    assert parse_all_events_link(html) is None


def test_the_first_good_events_link_wins() -> None:
    html = "".join(
        [
            '<a href="https://evil.example/en/people/x/events/9">no</a>',
            '<a href="https://www.rostercon.com/en/people/x/events/1">yes</a>',
            '<a href="https://www.rostercon.com/en/people/y/events/2">later</a>',
        ]
    )

    link = parse_all_events_link(html)

    assert link == "https://www.rostercon.com/en/people/x/events/1"


# --- turning a name into an address --------------------------------------------------


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Paul McGann", "paul-mcgann"),
        ("  Zoë Ball ", "zoe-ball"),
        ("A.L. Katz", "a-l-katz"),
        ("Jean-Luc Picard", "jean-luc-picard"),
        ("O'Brien", "o-brien"),
        ("José  Müller", "jose-muller"),
        ("Dangerous Bob Widin", "dangerous-bob-widin"),
    ],
)
def test_names_are_slugged_the_way_rostercon_does(name: str, slug: str) -> None:
    assert slugify_name(name) == slug


@pytest.mark.parametrize("name", ["", "   ", "!!!", "…"])
def test_a_name_with_no_letters_has_no_slug(name: str) -> None:
    assert slugify_name(name) == ""


@pytest.mark.parametrize(
    "name", ["a/../b", "x?y=1", "a b#c", "http://evil.example/", "..", "a\b"]
)
def test_a_slug_can_never_hold_a_path_a_query_or_a_host(name: str) -> None:
    assert set(slugify_name(name)) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")
