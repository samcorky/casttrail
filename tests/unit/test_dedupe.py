from datetime import date

import pytest

from casttrail.discovery.dedupe import (
    EventFacts,
    judge,
    normalise_event_name,
    similar_names,
)


def _facts(
    name: str = "Some Con",
    start: date | None = date(2027, 4, 17),
    end: date | None = date(2027, 4, 18),
) -> EventFacts:
    return EventFacts(name, start, end)


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Some Con", "some con"),
        ("  SOME   con ", "some con"),
        ("London Film & Comic Con", "london film and comic con"),
        ("London Film and Comic Con", "london film and comic con"),
        ("Follow The Yellow Gate Road (Take 4)", "follow the yellow gate road take 4"),
        ("Café-Con!", "cafe con"),
        ("", ""),
        ("日本", ""),
    ],
)
def test_names_are_reduced_to_comparable_words(raw: str, clean: str) -> None:
    assert normalise_event_name(raw) == clean


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("Comic Con Liverpool", "Liverpool Comic Con", True),
        ("Comic Con", "Comic Con Liverpool 2027", True),
        ("The Big Show", "Big Show", True),
        ("Comic Con Liverpool", "Comic Con Manchester", True),
        ("Dragon Con", "Starfury Weekend", False),
        ("Alpha Beta Gamma Delta", "Alpha Zeta Eta Theta", False),
        ("", "Comic Con", False),
        ("The", "The", False),
    ],
)
def test_alike_names_are_worth_asking_about(
    first: str, second: str, expected: bool
) -> None:
    assert similar_names(first, second) is expected


def test_identical_listings_are_the_same_event() -> None:
    assert judge(_facts(), _facts()) == "same"


def test_the_same_name_tidied_up_is_the_same_event() -> None:
    assert (
        judge(_facts("London Film & Comic Con"), _facts("london film and comic con!"))
        == "same"
    )


def test_a_missing_end_day_does_not_stop_a_match() -> None:
    assert judge(_facts(end=None), _facts()) == "same"
    assert judge(_facts(), _facts(end=None)) == "same"


def test_the_same_name_with_a_different_end_day_needs_a_person() -> None:
    assert judge(_facts(end=date(2027, 4, 19)), _facts()) == "maybe"


def test_the_same_name_starting_on_a_different_overlapping_day_needs_a_person() -> None:
    assert (
        judge(_facts(start=date(2027, 4, 18), end=date(2027, 4, 19)), _facts())
        == "maybe"
    )


def test_alike_names_on_the_same_days_need_a_person() -> None:
    assert judge(_facts("Some Con Lincoln"), _facts("Some Con")) == "maybe"


def test_unalike_names_are_different_even_on_the_same_days() -> None:
    assert judge(_facts("Dragon Con"), _facts("Starfury Weekend")) == "different"


def test_the_same_name_on_other_days_is_a_different_event() -> None:
    assert (
        judge(_facts(), _facts(start=date(2028, 4, 17), end=date(2028, 4, 18)))
        == "different"
    )


def test_events_one_day_apart_do_not_overlap() -> None:
    first = _facts(start=date(2027, 4, 17), end=date(2027, 4, 17))
    second = _facts(start=date(2027, 4, 18), end=date(2027, 4, 18))

    assert judge(first, second) == "different"


def test_events_that_touch_on_one_day_overlap() -> None:
    first = _facts(start=date(2027, 4, 17), end=date(2027, 4, 18))
    second = _facts(start=date(2027, 4, 18), end=date(2027, 4, 19))

    assert judge(first, second) == "maybe"


def test_an_unknown_date_with_the_same_name_needs_a_person() -> None:
    assert judge(_facts(start=None, end=None), _facts()) == "maybe"
    assert judge(_facts(), _facts(start=None, end=None)) == "maybe"


def test_an_unknown_date_with_another_name_is_different() -> None:
    assert judge(_facts("Dragon Con", None, None), _facts()) == "different"


def test_a_listing_whose_end_is_before_its_start_is_never_trusted_as_the_same() -> None:
    odd = _facts(start=date(2027, 4, 17), end=date(2027, 4, 1))

    assert judge(odd, _facts(end=date(2027, 4, 17))) == "maybe"


def test_the_same_name_and_end_day_but_a_different_start_day_needs_a_person() -> None:
    longer = _facts(start=date(2027, 4, 16), end=date(2027, 4, 18))

    assert judge(longer, _facts()) == "maybe"
