import uuid

import pytest

from casttrail.matching.resolver import (
    Candidate,
    Guest,
    Resolution,
    normalise_name,
    resolve,
)

PAUL = Candidate(uuid.uuid4(), "Paul McGann", {"tmdb": "1"})
SEAN = Candidate(uuid.uuid4(), "Sean Pertwee", {"tmdb": "2"})


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Paul McGann", "paul mcgann"),
        ("  PAUL   mcgann ", "paul mcgann"),
        ("Zoë Wanamaker", "zoe wanamaker"),
        ("Jean-Luc O'Neil", "jean luc o neil"),
        ("A. J. Cook", "a j cook"),
        ("", ""),
        ("日本", ""),
    ],
)
def test_names_are_reduced_to_comparable_words(raw: str, clean: str) -> None:
    assert normalise_name(raw) == clean


def test_a_link_made_by_hand_beats_everything() -> None:
    other = uuid.uuid4()
    guest = Guest(
        name="Paul McGann",
        identifiers={"tmdb": "1"},
        linked_person_id=other,
        rejected_person_ids=frozenset({other}),
    )

    assert resolve(guest, [PAUL]) == Resolution("manual", other, "Linked by hand.")


def test_a_shared_id_beats_a_name() -> None:
    guest = Guest(name="Someone Else", identifiers={"tmdb": "2"})

    result = resolve(guest, [PAUL, SEAN])

    assert (result.kind, result.person_id) == ("shared_id", SEAN.person_id)
    assert "tmdb" in result.reason


def test_a_different_id_with_the_same_name_is_still_only_possible() -> None:
    guest = Guest(name="Paul McGann", identifiers={"tmdb": "999"})

    assert resolve(guest, [PAUL]).kind == "possible"


def test_an_empty_id_never_matches() -> None:
    nameless = Candidate(uuid.uuid4(), "X", {"tmdb": ""})

    assert resolve(Guest(name=None, identifiers={"tmdb": ""}), [nameless]).kind == (
        "unresolved"
    )


def test_an_exact_name_is_only_possible_never_confirmed() -> None:
    result = resolve(Guest(name="paul  MCGANN"), [PAUL, SEAN])

    assert result == Resolution("possible", PAUL.person_id, "Same name.")


def test_accents_and_punctuation_do_not_stop_a_match() -> None:
    zoe = Candidate(uuid.uuid4(), "Zoe Wanamaker")

    assert resolve(Guest(name="Zoë Wanamaker"), [zoe]).person_id == zoe.person_id


def test_a_partial_name_does_not_match() -> None:
    assert resolve(Guest(name="Paul"), [PAUL]).kind == "unresolved"
    assert resolve(Guest(name="Paul McGann Jr"), [PAUL]).kind == "unresolved"


def test_two_watched_people_with_one_name_are_not_guessed() -> None:
    twin = Candidate(uuid.uuid4(), "Paul McGann")

    result = resolve(Guest(name="Paul McGann"), [PAUL, twin])

    assert (result.kind, result.person_id) == ("unresolved", None)
    assert "Several" in result.reason


def test_a_rejected_person_is_not_offered_again() -> None:
    guest = Guest(name="Paul McGann", rejected_person_ids=frozenset({PAUL.person_id}))

    assert resolve(guest, [PAUL]).kind == "unresolved"


def test_a_rejection_leaves_the_other_namesake_clear() -> None:
    twin = Candidate(uuid.uuid4(), "Paul McGann")
    guest = Guest(name="Paul McGann", rejected_person_ids=frozenset({PAUL.person_id}))

    result = resolve(guest, [PAUL, twin])

    assert (result.kind, result.person_id) == ("possible", twin.person_id)


def test_a_rejected_person_is_not_matched_by_id_either() -> None:
    guest = Guest(
        name="X",
        identifiers={"tmdb": "1"},
        rejected_person_ids=frozenset({PAUL.person_id}),
    )

    assert resolve(guest, [PAUL]).kind == "unresolved"


@pytest.mark.parametrize("name", [None, "", "   ", "!!!"])
def test_a_guest_without_a_usable_name_is_unresolved(name: str | None) -> None:
    result = resolve(Guest(name=name), [PAUL])

    assert result == Resolution("unresolved", None, "The guest has no name.")


def test_nobody_watched_means_unresolved() -> None:
    result = resolve(Guest(name="Paul McGann"), [])

    assert result == Resolution("unresolved", None, "No watched person has this name.")
