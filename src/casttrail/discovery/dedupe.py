import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Literal

Verdict = Literal["same", "maybe", "different"]

# Words that say little about which event it is.
_FILLER = frozenset({"the", "and", "of", "a", "an"})
# Share of words two names must have in common to be worth a person looking at.
_SIMILAR = 0.5


@dataclass(frozen=True)
class EventFacts:
    """What is needed to judge whether two listings are the same event."""

    name: str
    start_date: date | None
    end_date: date | None


def normalise_event_name(name: str) -> str:
    """Reduce an event name to what must be equal for two names to count as the same.

    Args:
        name: An event's name.

    Returns:
        Lower-case words with accents and punctuation removed, "&" read as "and".
    """
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    words = re.sub(r"[^a-z0-9]+", " ", plain.lower().replace("&", " and ")).split()
    return " ".join(words)


def _words(name: str) -> frozenset[str]:
    return frozenset(normalise_event_name(name).split()) - _FILLER


def similar_names(first: str, second: str) -> bool:
    """Say whether two names are close enough for a person to be asked.

    Args:
        first: One name.
        second: The other name.

    Returns:
        ``True`` if one name's words are all in the other, or at least half of all the
        words used are shared. Two names with no words are never similar.
    """
    a, b = _words(first), _words(second)
    if not a or not b:
        return False
    return a <= b or b <= a or len(a & b) / len(a | b) >= _SIMILAR


def _span(facts: EventFacts) -> tuple[date, date] | None:
    if facts.start_date is None:
        return None
    return facts.start_date, max(facts.end_date or facts.start_date, facts.start_date)


def judge(first: EventFacts, second: EventFacts) -> Verdict:
    """Decide whether two listings describe the same event.

    ``same`` is strict: the same name once tidied, the same start day, and end days
    that agree or are not both known. Anything weaker that could still be one event
    (the same name on overlapping days or with a day unknown, or alike names on
    overlapping days) is ``maybe`` and is left for a person. Different days, or names
    that are not alike, are ``different``.

    Args:
        first: One listing.
        second: The other listing.

    Returns:
        The verdict.
    """
    same_name = normalise_event_name(first.name) == normalise_event_name(second.name)
    a, b = _span(first), _span(second)
    if a is None or b is None:
        return "maybe" if same_name else "different"
    if a[0] > b[1] or b[0] > a[1]:
        return "different"
    if not same_name and not similar_names(first.name, second.name):
        return "different"
    if same_name and a[0] == b[0]:
        ends_agree = (
            first.end_date is None
            or second.end_date is None
            or first.end_date == second.end_date
        )
        if ends_agree:
            return "same"
    return "maybe"
