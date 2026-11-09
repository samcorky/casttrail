import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["manual", "shared_id", "possible", "unresolved"]


@dataclass(frozen=True)
class Candidate:
    """A person someone watches, as far as matching needs to know."""

    person_id: uuid.UUID
    name: str
    # Ids the person is known by elsewhere, such as {"tmdb": "120679"}.
    identifiers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Guest:
    """A guest as a listing source describes them."""

    name: str | None
    identifiers: Mapping[str, str] = field(default_factory=dict)
    # A person this guest was linked to by hand, if anyone did.
    linked_person_id: uuid.UUID | None = None
    # People a person has said this guest is not.
    rejected_person_ids: frozenset[uuid.UUID] = frozenset()


@dataclass(frozen=True)
class Resolution:
    """Who a guest is, how sure that is, and why."""

    kind: Kind
    person_id: uuid.UUID | None
    reason: str


def normalise_name(name: str) -> str:
    """Reduce a name to what must be equal for two names to count as the same.

    Args:
        name: A person's name.

    Returns:
        Lower-case words with accents and punctuation removed, joined by single spaces.
    """
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", plain.lower()).split())


def resolve(guest: Guest, candidates: Iterable[Candidate]) -> Resolution:
    """Decide who a guest is, from the strongest evidence to the weakest.

    In order: a link made by hand; an id both sides share; an exact name match with a
    watched person, which is only ever *possible*; otherwise unresolved. A person the
    guest was rejected as is never offered again. A name shared by several watched
    people is not guessed at.

    Args:
        guest: The guest.
        candidates: The people being watched.

    Returns:
        The resolution.
    """
    if guest.linked_person_id is not None:
        return Resolution("manual", guest.linked_person_id, "Linked by hand.")
    allowed = [c for c in candidates if c.person_id not in guest.rejected_person_ids]
    for candidate in allowed:
        for provider, value in guest.identifiers.items():
            if value and candidate.identifiers.get(provider) == value:
                return Resolution(
                    "shared_id", candidate.person_id, f"Same {provider} id."
                )
    wanted = normalise_name(guest.name or "")
    if not wanted:
        return Resolution("unresolved", None, "The guest has no name.")
    same = [c for c in allowed if normalise_name(c.name) == wanted]
    if len(same) == 1:
        return Resolution("possible", same[0].person_id, "Same name.")
    if len(same) > 1:
        return Resolution("unresolved", None, "Several watched people have this name.")
    return Resolution("unresolved", None, "No watched person has this name.")
