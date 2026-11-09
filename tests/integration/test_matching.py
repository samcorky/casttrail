import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from casttrail.auth.service import AuthService
from casttrail.catalogue.models import Person
from casttrail.discovery.models import Appearance, AppearanceSource, Convention
from casttrail.identity.decisions import DecisionService
from casttrail.identity.models import ExternalIdentity
from casttrail.matching.models import Match
from casttrail.matching.service import MatchService, RematchResult, match_key_for_person
from casttrail.providers.base import PersonDetail
from casttrail.watching.models import Watch
from casttrail.watching.service import WatchService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.auth.models import User

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 9, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.current = NOW

    def __call__(self) -> datetime:
        self.current += timedelta(minutes=1)
        return self.current


async def _user(session: AsyncSession, email: str) -> User:
    auth = AuthService(session, session_ttl=timedelta(hours=1))
    return await auth.create_user(
        email=email, display_name=email, password="correct horse battery"
    )


async def _watch(session: AsyncSession, user: User, name: str, tmdb_id: int) -> Person:
    watch = await WatchService(session).watch_person(
        user.id, PersonDetail(tmdb_id=tmdb_id, name=name)
    )
    person = await session.scalar(select(Person).where(Person.id == watch.person_id))
    assert person is not None
    return person


async def _guest(
    session: AsyncSession,
    key: str,
    name: str | None,
    *,
    listed: bool = True,
    convention: Convention | None = None,
) -> Appearance:
    if convention is None:
        convention = Convention(
            id=uuid.uuid4(), name=f"Con {key}", created_at=NOW, updated_at=NOW
        )
        session.add(convention)
    identity = ExternalIdentity(
        id=uuid.uuid4(), provider="rostercon", external_id=key, display_name=name
    )
    session.add(identity)
    await session.flush()
    appearance = Appearance(
        id=uuid.uuid4(), convention_id=convention.id, external_identity_id=identity.id
    )
    session.add(appearance)
    await session.flush()
    session.add(
        AppearanceSource(
            appearance_id=appearance.id,
            provider="rostercon",
            source_url=f"https://www.rostercon.com/en/people/{key}",
            listed=listed,
        )
    )
    await session.commit()
    return appearance


async def _matches(session: AsyncSession) -> list[Match]:
    return list(
        await session.scalars(select(Match).order_by(Match.created_at, Match.id))
    )


@pytest.fixture
async def users(db_session: AsyncSession) -> tuple[User, User]:
    return await _user(db_session, "a@example.com"), await _user(
        db_session, "b@example.com"
    )


# --- the table --------------------------------------------------------------------


async def test_a_match_needs_a_key_and_a_known_kind(
    db_session: AsyncSession, users: tuple[User, User]
) -> None:
    person = await _watch(db_session, users[0], "Paul McGann", 1)
    appearance = await _guest(db_session, "p-en", "Paul McGann")
    user_id, appearance_id, person_id = users[0].id, appearance.id, person.id

    def row(**changes: object) -> Match:
        values: dict[str, object] = {
            "user_id": user_id,
            "appearance_id": appearance_id,
            "person_id": person_id,
            "match_key": "person:x",
            "kind": "possible",
            "reason": "r",
        }
        return Match(**{**values, **changes})

    for bad in ({"match_key": ""}, {"kind": "sure"}, {"kind": ""}):
        db_session.add(row(**bad))
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()

    db_session.add(row())
    await db_session.commit()
    db_session.add(row())
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
    db_session.add(row(match_key="person:y"))
    await db_session.commit()


async def test_removing_a_user_appearance_or_person_removes_their_matches(
    db_session: AsyncSession, users: tuple[User, User]
) -> None:
    a = await _watch(db_session, users[0], "Paul McGann", 1)
    await _watch(db_session, users[1], "Paul McGann", 1)
    one = await _guest(db_session, "p-en", "Paul McGann")
    await MatchService(db_session).rematch()
    assert len(await _matches(db_session)) == 2

    await db_session.delete(users[1])
    await db_session.commit()
    assert len(await _matches(db_session)) == 1

    await db_session.delete(one)
    await db_session.commit()
    assert await _matches(db_session) == []
    assert a is not None


async def test_unwatching_keeps_nothing_dangling(
    db_session: AsyncSession, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0], "Paul McGann", 1)
    await _guest(db_session, "p-en", "Paul McGann")
    service = MatchService(db_session)
    await service.rematch()
    watch = await db_session.scalar(select(Watch))
    assert watch is not None

    await WatchService(db_session).unwatch(users[0].id, watch.id)
    stored = (await _matches(db_session))[0]
    assert stored.watch_id is None

    result = await service.rematch()

    assert (result.removed, await _matches(db_session)) == (1, [])


# --- matching ---------------------------------------------------------------------


async def test_a_watched_name_gives_a_possible_match_with_its_reason(
    db_session: AsyncSession, users: tuple[User, User]
) -> None:
    person = await _watch(db_session, users[0], "Paul McGann", 1)
    appearance = await _guest(db_session, "p-en", "paul  mcgann")

    result = await MatchService(db_session).rematch()

    assert (result.added, result.updated, result.removed) == (1, 0, 0)
    (match,) = await _matches(db_session)
    assert (match.user_id, match.appearance_id, match.person_id) == (
        users[0].id,
        appearance.id,
        person.id,
    )
    assert (match.kind, match.reason) == ("possible", "Same name.")
    assert match.match_key == match_key_for_person(person.id)
    assert match.watch_id is not None


async def test_a_guest_nobody_watches_matches_nobody(
    db_session: AsyncSession, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0], "Paul McGann", 1)
    await _guest(db_session, "x-en", "Someone Else")
    await _guest(db_session, "n-en", None)

    result = await MatchService(db_session).rematch()

    assert result.added == 0


async def test_nothing_watched_does_nothing(db_session: AsyncSession) -> None:
    await _guest(db_session, "p-en", "Paul McGann")

    assert await MatchService(db_session).rematch() == RematchResult(0, 0, 0)


async def test_each_user_gets_matches_only_for_their_own_watches(
    db_session: AsyncSession, users: tuple[User, User]
) -> None:
    paul = await _watch(db_session, users[0], "Paul McGann", 1)
    await _watch(db_session, users[1], "Sean Pertwee", 2)
    await _guest(db_session, "p-en", "Paul McGann")
    await _guest(db_session, "s-en", "Sean Pertwee")

    await MatchService(db_session).rematch()

    got = {(m.user_id, m.person_id) for m in await _matches(db_session)}
    assert len(got) == 2
    assert (users[0].id, paul.id) in got
    assert all(
        user_id != users[1].id or person_id != paul.id for user_id, person_id in got
    )


async def test_two_users_watching_one_person_each_get_their_own_match(
    db_session: AsyncSession, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0], "Paul McGann", 1)
    await _watch(db_session, users[1], "Paul McGann", 1)
    await _guest(db_session, "p-en", "Paul McGann")

    await MatchService(db_session).rematch()

    assert {m.user_id for m in await _matches(db_session)} == {users[0].id, users[1].id}


async def test_a_guest_no_longer_listed_has_no_match(
    db_session: AsyncSession, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0], "Paul McGann", 1)
    await _guest(db_session, "p-en", "Paul McGann", listed=False)

    assert (await MatchService(db_session).rematch()).added == 0


async def test_a_guest_who_is_listed_again_gets_the_match_back(
    db_session: AsyncSession, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0], "Paul McGann", 1)
    await _guest(db_session, "p-en", "Paul McGann")
    service = MatchService(db_session)
    await service.rematch()
    line = await db_session.scalar(select(AppearanceSource))
    assert line is not None
    line.listed = False
    await db_session.commit()
    assert (await service.rematch()).removed == 1

    line.listed = True
    await db_session.commit()

    assert (await service.rematch()).added == 1


async def test_two_watched_namesakes_are_not_guessed(
    db_session: AsyncSession, users: tuple[User, User]
) -> None:
    await _watch(db_session, users[0], "Paul McGann", 1)
    await _watch(db_session, users[0], "Paul McGann", 2)
    await _guest(db_session, "p-en", "Paul McGann")

    assert (await MatchService(db_session).rematch()).added == 0


# --- decisions --------------------------------------------------------------------


async def test_confirming_makes_the_match_confirmed(
    db_session: AsyncSession, users: tuple[User, User]
) -> None:
    person = await _watch(db_session, users[0], "Paul McGann", 1)
    appearance = await _guest(db_session, "p-en", "Paul McGann")
    service = MatchService(db_session)
    await service.rematch()
    before = (await _matches(db_session))[0]
    created, updated_before = before.created_at, before.updated_at

    await DecisionService(db_session).confirm(
        appearance.external_identity_id, person.id, users[0].id
    )
    result = await service.rematch()

    assert (result.added, result.updated) == (0, 1)
    (match,) = await _matches(db_session)
    assert (match.kind, match.reason) == ("confirmed", "Linked by hand.")
    assert match.created_at == created
    assert match.updated_at > updated_before


async def test_a_confirmed_link_is_only_visible_to_users_who_watch_that_person(
    db_session: AsyncSession, users: tuple[User, User]
) -> None:
    paul = await _watch(db_session, users[0], "Paul McGann", 1)
    await _watch(db_session, users[1], "Sean Pertwee", 2)
    appearance = await _guest(db_session, "p-en", "Sean Pertwee")
    await DecisionService(db_session).confirm(
        appearance.external_identity_id, paul.id, users[0].id
    )

    await MatchService(db_session).rematch()

    matches = await _matches(db_session)
    assert [(m.user_id, m.person_id, m.kind) for m in matches] == [
        (users[0].id, paul.id, "confirmed")
    ]


async def test_rejecting_removes_the_match_and_it_stays_gone(
    db_session: AsyncSession, users: tuple[User, User]
) -> None:
    person = await _watch(db_session, users[0], "Paul McGann", 1)
    appearance = await _guest(db_session, "p-en", "Paul McGann")
    service = MatchService(db_session)
    await service.rematch()

    await DecisionService(db_session).reject(
        appearance.external_identity_id, person.id, users[0].id
    )
    first = await service.rematch()
    second = await service.rematch()

    assert first.removed == 1
    assert second == RematchResult(0, 0, 0)
    assert await _matches(db_session) == []


async def test_a_rejection_hides_the_guest_for_every_user(
    db_session: AsyncSession, users: tuple[User, User]
) -> None:
    person = await _watch(db_session, users[0], "Paul McGann", 1)
    await _watch(db_session, users[1], "Paul McGann", 1)
    appearance = await _guest(db_session, "p-en", "Paul McGann")
    await DecisionService(db_session).reject(
        appearance.external_identity_id, person.id, users[0].id
    )

    await MatchService(db_session).rematch()

    assert await _matches(db_session) == []


# --- idempotence ------------------------------------------------------------------


async def test_rematching_changes_nothing_the_second_time(
    db_session: AsyncSession, users: tuple[User, User]
) -> None:
    clock = Clock()
    await _watch(db_session, users[0], "Paul McGann", 1)
    await _guest(db_session, "p-en", "Paul McGann")
    service = MatchService(db_session, now=clock)
    await service.rematch()
    before = [(m.id, m.updated_at) for m in await _matches(db_session)]

    result = await service.rematch()

    assert (result.added, result.updated, result.removed) == (0, 0, 0)
    assert [(m.id, m.updated_at) for m in await _matches(db_session)] == before
    assert await db_session.scalar(select(func.count()).select_from(Match)) == 1


async def test_a_failed_rematch_changes_nothing(
    db_session: AsyncSession,
    users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_id = users[0].id
    await _watch(db_session, users[0], "Paul McGann", 1)
    await _guest(db_session, "p-en", "Paul McGann")
    service = MatchService(db_session)
    await service.rematch()
    watch = await db_session.scalar(select(Watch))
    assert watch is not None
    await WatchService(db_session).unwatch(users[0].id, watch.id)
    await _watch(db_session, users[1], "Paul McGann", 1)

    async def boom(*args: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(db_session, "delete", boom)
    with pytest.raises(RuntimeError, match="boom"):
        await service.rematch()
    monkeypatch.undo()

    stored = await _matches(db_session)
    assert [m.user_id for m in stored] == [first_id]
