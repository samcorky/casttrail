import json
from datetime import date
from pathlib import Path

import pytest

from casttrail.providers.base import ProviderError
from casttrail.providers.comiconomicon import (
    MAX_SUGGESTIONS,
    Suggestion,
    match_by_name,
    parse_autocomplete,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "comiconomicon"
# Digits that look like ASCII ones but are not, which must not count as a year.
FULLWIDTH_1984 = "".join(chr(code) for code in (0xFF11, 0xFF19, 0xFF18, 0xFF14))


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _entry(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": "518",
        "name": "Andrew Jackson",
        "type": "guest",
        "data1": None,
        "data2": None,
        "data3": None,
        "appearances": 1,
        "new": 0,
        "url": "/guest/518/Andrew_Jackson",
    }
    return {**values, **changes}


def _parse(*entries: object) -> list[Suggestion]:
    return parse_autocomplete(json.dumps(list(entries))).items


# --- the real answers -------------------------------------------------------------


def test_a_guest_result_gives_their_id_page_and_upcoming_count() -> None:
    result = parse_autocomplete(_fixture("autocomplete_guest_andrew_jackson.json"))

    assert result.skipped == 0
    assert result.items == [
        Suggestion(
            kind="guest",
            source_key="518",
            source_url="https://comiconomicon.com/guest/518/Andrew_Jackson",
            name="Andrew Jackson",
            appearances=1,
        )
    ]


def test_a_film_result_has_its_year() -> None:
    result = parse_autocomplete(_fixture("autocomplete_videomedia_top_secret.json"))

    assert result.skipped == 0
    first, second = result.items
    assert (first.kind, first.source_key, first.name, first.year) == (
        "videomedia",
        "2540",
        "Top Secret!",
        1984,
    )
    assert first.source_url == "https://comiconomicon.com/videomedia/2540/Top_Secret_"
    assert first.appearances == 1
    assert (second.name, second.year, second.appearances) == (
        "Top Secret UFO Projects: Declassified",
        2021,
        0,
    )


def test_an_event_result_has_its_days_and_place() -> None:
    (event,) = parse_autocomplete(_fixture("autocomplete_event_yellow_gate.json")).items

    assert event.kind == "event"
    assert event.source_key == "42676"
    assert event.name == "Follow The Yellow Gate Road (Take 4)"
    assert (event.start_date, event.end_date) == (date(2027, 4, 17), date(2027, 4, 18))
    assert event.location is not None
    assert "Lincoln" in event.location
    assert event.appearances == 6


def test_an_empty_answer_is_no_results() -> None:
    result = parse_autocomplete("[]")

    assert (result.items, result.skipped) == ([], 0)


# --- an answer we do not understand is an error, not "no results" -----------------


@pytest.mark.parametrize(
    "body",
    [
        "",
        "not json",
        "<html>blocked</html>",
        "{}",
        '{"id": "1"}',
        '"text"',
        "null",
        "42",
    ],
)
def test_an_answer_that_is_not_a_list_is_an_error(body: str) -> None:
    with pytest.raises(ProviderError):
        parse_autocomplete(body)


def test_the_error_names_no_address() -> None:
    with pytest.raises(ProviderError) as error:
        parse_autocomplete("nope")

    assert "comiconomicon.com" not in str(error.value)
    assert "http" not in str(error.value)


# --- entries we refuse ------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        "a string",
        42,
        None,
        [1, 2],
        _entry(type="person"),
        _entry(type=None),
        _entry(type="Guest"),
        _entry(id=518),
        _entry(id="abc", url="/guest/abc/x"),
        _entry(name=""),
        _entry(name="   "),
        _entry(name=None),
        _entry(name=["x"]),
        _entry(url=None),
        _entry(url=""),
        _entry(url="https://evil.test/guest/518/x"),
        _entry(url="//evil.test/guest/518/x"),
        _entry(url="/guest/518/../../etc"),
        _entry(url="/guest/518/x?next=http://evil.test"),
        _entry(url="/guest/518/x#frag"),
        _entry(url="javascript:alert(1)"),
        _entry(url="/guest/1234567890/x"),
        _entry(url="/guest/519/Andrew_Jackson"),
        _entry(url="/videomedia/518/x"),
        _entry(type="event", url="/guest/518/x"),
    ],
)
def test_an_entry_that_is_not_what_it_claims_is_skipped_and_counted(
    entry: object,
) -> None:
    result = parse_autocomplete(json.dumps([entry]))

    assert result.items == []
    assert result.skipped == 1


def test_one_bad_entry_does_not_spoil_the_good_ones() -> None:
    result = parse_autocomplete(
        json.dumps(
            [
                _entry(),
                _entry(url="https://evil.test/x"),
                _entry(id="9", url="/guest/9/B", name="B"),
            ]
        )
    )

    assert [i.source_key for i in result.items] == ["518", "9"]
    assert result.skipped == 1


def test_the_same_result_twice_is_kept_once_with_the_first_word() -> None:
    items = _parse(_entry(), _entry(name="Renamed"))

    assert [i.name for i in items] == ["Andrew Jackson"]


def test_a_guest_and_a_film_with_the_same_number_are_different_things() -> None:
    items = _parse(
        _entry(),
        _entry(type="videomedia", url="/videomedia/518/Some_Film", name="Some Film"),
    )

    assert [(i.kind, i.source_key) for i in items] == [
        ("guest", "518"),
        ("videomedia", "518"),
    ]


def test_markup_in_a_name_stays_text() -> None:
    (item,) = _parse(_entry(name="<script>alert(1)</script>  A &amp; B"))

    assert item.name == "<script>alert(1)</script> A &amp; B"


def test_the_html_the_site_sends_for_its_drop_down_is_ignored() -> None:
    (item,) = _parse(_entry(html="<img src=x onerror=alert(1)>"))

    assert not hasattr(item, "html")
    assert item.name == "Andrew Jackson"


@pytest.mark.parametrize("value", [0, -3, "4", None, 1.5, True])
def test_an_appearance_count_is_a_positive_whole_number_or_zero(
    value: object,
) -> None:
    (item,) = _parse(_entry(appearances=value))

    assert item.appearances == (1 if value is True else 0)


@pytest.mark.parametrize(
    ("data1", "year"),
    [
        ("1984", 1984),
        (1984, 1984),
        (" 2021 ", 2021),
        ("84", None),
        ("19840", None),
        ("abcd", None),
        (None, None),
        ("", None),
        (FULLWIDTH_1984, None),
    ],
)
def test_only_a_plain_four_digit_year_is_a_year(
    data1: object, year: int | None
) -> None:
    (item,) = _parse(
        _entry(type="videomedia", url="/videomedia/518/F", name="F", data1=data1)
    )

    assert item.year == year


@pytest.mark.parametrize(
    ("data1", "data2", "start", "end"),
    [
        ("2027-04-17", "2027-04-18", date(2027, 4, 17), date(2027, 4, 18)),
        ("2027-04-17", None, date(2027, 4, 17), None),
        ("17 Apr 2027", "tomorrow", None, None),
        ("2027-13-45", "2027-02-30", None, None),
        (20270417, None, None, None),
    ],
)
def test_event_days_must_be_real_iso_dates(
    data1: object, data2: object, start: date | None, end: date | None
) -> None:
    (item,) = _parse(
        _entry(type="event", url="/event/518/E", name="E", data1=data1, data2=data2)
    )

    assert (item.start_date, item.end_date) == (start, end)


def test_an_event_place_is_one_tidy_line_or_unknown() -> None:
    (with_place, without) = _parse(
        _entry(
            type="event", url="/event/1/E", id="1", name="E", data3="A  Hall\nLincoln"
        ),
        _entry(type="event", url="/event/2/F", id="2", name="F", data3=""),
    )

    assert with_place.location == "A Hall Lincoln"
    assert without.location is None


def test_a_huge_answer_is_cut_off_and_the_rest_counted() -> None:
    entries = [
        _entry(id=str(n), url=f"/guest/{n}/G", name=f"Guest {n}")
        for n in range(MAX_SUGGESTIONS + 25)
    ]

    result = parse_autocomplete(json.dumps(entries))

    assert len(result.items) == MAX_SUGGESTIONS
    assert result.skipped == 25


# --- picking the right result for a name ------------------------------------------


def _item(kind: str, name: str, key: str = "1", year: int | None = None) -> Suggestion:
    return Suggestion(
        kind, key, f"https://comiconomicon.com/{kind}/{key}", name, 0, year=year
    )


def test_an_exact_name_is_found_once() -> None:
    items = [
        _item("guest", "Andrew Jackson", "518"),
        _item("guest", "Andrew Jacksonville", "9"),
    ]

    assert [m.source_key for m in match_by_name(items, "guest", "andrew  JACKSON")] == [
        "518"
    ]


def test_accents_and_punctuation_do_not_stop_a_match() -> None:
    items = [
        _item("guest", "Zoe Wanamaker", "1"),
        _item("videomedia", "Top Secret!", "2"),
    ]

    assert len(match_by_name(items, "guest", "Zoë Wanamaker")) == 1
    assert len(match_by_name(items, "videomedia", "top secret")) == 1


def test_and_and_an_ampersand_are_the_same() -> None:
    items = [_item("event", "London Film & Comic Con", "1")]

    assert len(match_by_name(items, "event", "London Film and Comic Con")) == 1


def test_a_partial_name_does_not_match() -> None:
    items = [_item("guest", "Andrew Jackson", "518")]

    assert match_by_name(items, "guest", "Andrew") == []
    assert match_by_name(items, "guest", "Andrew Jackson Jr") == []


def test_only_the_kind_asked_for_is_considered() -> None:
    items = [
        _item("videomedia", "Andrew Jackson", "1"),
        _item("guest", "Andrew Jackson", "2"),
    ]

    assert [m.source_key for m in match_by_name(items, "guest", "Andrew Jackson")] == [
        "2"
    ]


def test_two_results_with_one_name_are_both_returned_for_the_caller_to_refuse() -> None:
    items = [_item("guest", "Paul Smith", "1"), _item("guest", "Paul Smith", "2")]

    assert len(match_by_name(items, "guest", "Paul Smith")) == 2


def test_a_year_must_agree_when_given() -> None:
    items = [
        _item("videomedia", "Top Secret!", "1", year=1984),
        _item("videomedia", "Top Secret!", "2", year=2020),
        _item("videomedia", "Top Secret!", "3", year=None),
    ]

    assert [
        m.source_key
        for m in match_by_name(items, "videomedia", "Top Secret!", year=1984)
    ] == ["1"]
    assert len(match_by_name(items, "videomedia", "Top Secret!")) == 3


@pytest.mark.parametrize("name", ["", "   ", "!!!", "日本"])
def test_a_name_with_no_letters_matches_nothing(name: str) -> None:
    items = [_item("guest", "", "1"), _item("guest", "!!!", "2")]

    assert match_by_name(items, "guest", name) == []


def test_no_results_means_not_found() -> None:
    assert match_by_name([], "guest", "Andrew Jackson") == []
