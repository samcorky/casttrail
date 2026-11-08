import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from casttrail.auth.service import AuthService
from casttrail.catalogue.models import Person, Production
from casttrail.identity.models import ExternalIdentity
from casttrail.providers.base import PersonDetail, Production as ProductionData
from casttrail.watching.models import Watch
from casttrail.watching.service import WatchService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from casttrail.auth.models import User

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"

EDDIE = PersonDetail(
    tmdb_id=120679,
    name="Eddie McClintock",
    known_for_department="Acting",
    profile_path="/face.jpg",
    imdb_id="nm0565858",
)
WAREHOUSE = ProductionData(
    tmdb_id=15819,
    media_type="tv",
    title="Warehouse 13",
    release_year=2009,
    poster_path="/w13.jpg",
)
SKYFALL = ProductionData(
    tmdb_id=37724, media_type="movie", title="Skyfall", release_year=2012
)


async def _count(session: AsyncSession, model: Any) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


@pytest.fixture
async def users(db_session: AsyncSession) -> tuple[User, User]:
    auth = AuthService(db_session, session_ttl=timedelta(hours=1))
    alice = await auth.create_user(
        email="alice@example.com", display_name="Alice", password=PASSWORD
    )
    bob = await auth.create_user(
        email="bob@example.com", display_name="Bob", password=PASSWORD
    )
    return alice, bob


@pytest.fixture
def service(db_session: AsyncSession) -> WatchService:
    return WatchService(db_session)


# --- watching a person ----------------------------------------------------------


async def test_watching_a_person_records_them_and_both_of_their_ids(
    service: WatchService, db_session: AsyncSession, users: tuple[User, User]
) -> None:
    alice, _ = users

    watch = await service.watch_person(alice.id, EDDIE)

    person = await db_session.scalar(select(Person))
    assert person is not None
    assert (person.name, person.known_for_department) == ("Eddie McClintock", "Acting")
    assert person.profile_path == "/face.jpg"
    assert (watch.user_id, watch.person_id, watch.production_id) == (
        alice.id,
        person.id,
        None,
    )
    identities = await db_session.scalars(select(ExternalIdentity))
    assert {(i.provider, i.external_id, i.person_id) for i in identities} == {
        ("tmdb", "120679", person.id),
        ("imdb", "nm0565858", person.id),
    }


async def test_watching_without_an_imdb_id_records_only_the_tmdb_id(
    service: WatchService, db_session: AsyncSession, users: tuple[User, User]
) -> None:
    await service.watch_person(
        users[0].id, PersonDetail(tmdb_id=5, name="No Imdb", imdb_id=None)
    )

    providers = await db_session.scalars(select(ExternalIdentity.provider))
    assert list(providers) == ["tmdb"]


async def test_watching_the_same_person_twice_changes_nothing(
    service: WatchService, db_session: AsyncSession, users: tuple[User, User]
) -> None:
    first = await service.watch_person(users[0].id, EDDIE)
    second = await service.watch_person(users[0].id, EDDIE)

    assert second.id == first.id
    assert await _count(db_session, Watch) == 1
    assert await _count(db_session, Person) == 1
    assert await _count(db_session, ExternalIdentity) == 2


async def test_two_users_share_the_person_but_not_the_watch(
    service: WatchService, db_session: AsyncSession, users: tuple[User, User]
) -> None:
    alice, bob = users

    a = await service.watch_person(alice.id, EDDIE)
    b = await service.watch_person(bob.id, EDDIE)

    assert a.id != b.id
    assert a.person_id == b.person_id
    assert await _count(db_session, Person) == 1
    assert await _count(db_session, Watch) == 2


async def test_watching_refreshes_what_the_source_says_about_the_person(
    service: WatchService, db_session: AsyncSession, users: tuple[User, User]
) -> None:
    await service.watch_person(users[0].id, EDDIE)

    await service.watch_person(
        users[1].id,
        EDDIE.model_copy(update={"name": "Edward McClintock", "profile_path": None}),
    )

    person = await db_session.scalar(select(Person))
    assert person is not None
    assert (person.name, person.profile_path) == ("Edward McClintock", None)


async def test_an_unlinked_identity_is_linked_when_the_person_is_watched(
    service: WatchService, db_session: AsyncSession, users: tuple[User, User]
) -> None:
    db_session.add(ExternalIdentity(provider="tmdb", external_id="120679"))
    await db_session.commit()

    await service.watch_person(users[0].id, EDDIE)

    identity = await db_session.scalar(
        select(ExternalIdentity).where(ExternalIdentity.provider == "tmdb")
    )
    person = await db_session.scalar(select(Person))
    assert person is not None
    assert identity is not None
    assert identity.person_id == person.id
    assert await _count(db_session, ExternalIdentity) == 2


async def test_an_unlinked_imdb_identity_is_linked_when_the_person_is_watched(
    service: WatchService, db_session: AsyncSession, users: tuple[User, User]
) -> None:
    db_session.add(ExternalIdentity(provider="imdb", external_id="nm0565858"))
    await db_session.commit()

    await service.watch_person(users[0].id, EDDIE)

    imdb = await db_session.scalar(
        select(ExternalIdentity).where(ExternalIdentity.provider == "imdb")
    )
    person = await db_session.scalar(select(Person))
    assert person is not None
    assert imdb is not None
    assert imdb.person_id == person.id
    assert await _count(db_session, ExternalIdentity) == 2


async def test_an_identity_already_linked_to_someone_is_never_moved(
    service: WatchService, db_session: AsyncSession, users: tuple[User, User]
) -> None:
    linked = Person(name="Linked By Hand")
    other = Person(name="Someone Else")
    db_session.add_all([linked, other])
    await db_session.flush()
    db_session.add_all(
        [
            ExternalIdentity(
                provider="tmdb", external_id="120679", person_id=linked.id
            ),
            ExternalIdentity(
                provider="imdb", external_id="nm0565858", person_id=other.id
            ),
        ]
    )
    await db_session.commit()

    watch = await service.watch_person(users[0].id, EDDIE)

    assert watch.person_id == linked.id
    imdb = await db_session.scalar(
        select(ExternalIdentity).where(ExternalIdentity.provider == "imdb")
    )
    assert imdb is not None
    assert imdb.person_id == other.id
    assert await _count(db_session, Person) == 2


# --- watching a film or series --------------------------------------------------


async def test_watching_a_series_records_it(
    service: WatchService, db_session: AsyncSession, users: tuple[User, User]
) -> None:
    watch = await service.watch_production(users[0].id, WAREHOUSE)

    production = await db_session.scalar(select(Production))
    assert production is not None
    assert (production.media_type, production.tmdb_id, production.title) == (
        "tv",
        15819,
        "Warehouse 13",
    )
    assert (production.release_year, production.poster_path) == (2009, "/w13.jpg")
    assert (watch.production_id, watch.person_id) == (production.id, None)


async def test_watching_the_same_production_twice_changes_nothing(
    service: WatchService, db_session: AsyncSession, users: tuple[User, User]
) -> None:
    first = await service.watch_production(users[0].id, WAREHOUSE)
    second = await service.watch_production(users[0].id, WAREHOUSE)

    assert second.id == first.id
    assert await _count(db_session, Watch) == 1
    assert await _count(db_session, Production) == 1


async def test_a_film_and_a_series_with_the_same_number_are_different_things(
    service: WatchService, db_session: AsyncSession, users: tuple[User, User]
) -> None:
    await service.watch_production(users[0].id, SKYFALL)
    await service.watch_production(
        users[0].id, SKYFALL.model_copy(update={"media_type": "tv", "title": "A Show"})
    )

    assert await _count(db_session, Production) == 2
    assert await _count(db_session, Watch) == 2


async def test_watching_refreshes_what_the_source_says_about_the_production(
    service: WatchService, db_session: AsyncSession, users: tuple[User, User]
) -> None:
    await service.watch_production(users[0].id, WAREHOUSE)

    await service.watch_production(
        users[1].id,
        WAREHOUSE.model_copy(
            update={"title": "Warehouse Thirteen", "poster_path": None}
        ),
    )

    production = await db_session.scalar(select(Production))
    assert production is not None
    assert (production.title, production.poster_path) == ("Warehouse Thirteen", None)


# --- reading --------------------------------------------------------------------


async def test_a_user_sees_only_their_own_watches_newest_first(
    service: WatchService, db_session: AsyncSession, users: tuple[User, User]
) -> None:
    alice, bob = users
    older = await service.watch_person(alice.id, EDDIE)
    newer = await service.watch_production(alice.id, WAREHOUSE)
    await service.watch_production(bob.id, SKYFALL)
    now = datetime.now(UTC)
    await db_session.execute(
        update(Watch)
        .where(Watch.id == older.id)
        .values(created_at=now - timedelta(days=1))
    )
    await db_session.execute(
        update(Watch).where(Watch.id == newer.id).values(created_at=now)
    )
    await db_session.commit()

    watches = await service.list_watches(alice.id)

    assert [w.id for w in watches] == [newer.id, older.id]
    assert watches[0].production is not None
    assert watches[0].production.title == "Warehouse 13"
    assert watches[1].person is not None
    assert watches[1].person.name == "Eddie McClintock"
    assert await service.list_watches(uuid.uuid4()) == []


async def test_the_watched_maps_are_per_user(
    service: WatchService, users: tuple[User, User]
) -> None:
    alice, bob = users
    person_watch = await service.watch_person(alice.id, EDDIE)
    production_watch = await service.watch_production(alice.id, WAREHOUSE)

    assert await service.watched_people(alice.id) == {120679: person_watch.id}
    assert await service.watched_productions(alice.id) == {
        ("tv", 15819): production_watch.id
    }
    assert await service.watched_people(bob.id) == {}
    assert await service.watched_productions(bob.id) == {}


# --- unwatching -----------------------------------------------------------------


async def test_a_user_can_stop_watching(
    service: WatchService, db_session: AsyncSession, users: tuple[User, User]
) -> None:
    watch = await service.watch_person(users[0].id, EDDIE)

    assert await service.unwatch(users[0].id, watch.id) is True

    assert await _count(db_session, Watch) == 0
    assert await _count(db_session, Person) == 1


async def test_a_user_cannot_remove_someone_elses_watch(
    service: WatchService, db_session: AsyncSession, users: tuple[User, User]
) -> None:
    alice, bob = users
    watch = await service.watch_person(alice.id, EDDIE)

    assert await service.unwatch(bob.id, watch.id) is False

    assert await _count(db_session, Watch) == 1


async def test_removing_a_watch_that_does_not_exist_is_false(
    service: WatchService, users: tuple[User, User]
) -> None:
    assert await service.unwatch(users[0].id, uuid.uuid4()) is False


async def test_watching_again_after_stopping_works(
    service: WatchService, db_session: AsyncSession, users: tuple[User, User]
) -> None:
    first = await service.watch_person(users[0].id, EDDIE)
    await service.unwatch(users[0].id, first.id)

    second = await service.watch_person(users[0].id, EDDIE)

    assert second.id != first.id
    assert await _count(db_session, Watch) == 1


# --- concurrency ----------------------------------------------------------------


async def test_a_watch_made_at_the_same_moment_by_another_request_is_returned(
    service: WatchService,
    db_session: AsyncSession,
    users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice, _ = users
    existing = await service.watch_person(alice.id, EDDIE)
    real_scalar = db_session.scalar
    misses = 0

    async def blind_once(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal misses
        if "FROM watches" in str(statement) and misses == 0:
            misses += 1
            return None  # as if the other request had not committed yet
        return await real_scalar(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "scalar", blind_once)

    watch = await service.watch_person(alice.id, EDDIE)

    assert misses == 1
    assert watch.id == existing.id
    assert await _count(db_session, Watch) == 1


# --- what the database itself refuses -------------------------------------------


async def test_a_watch_needs_exactly_one_target(
    db_session: AsyncSession, users: tuple[User, User]
) -> None:
    alice_id = users[0].id
    person = Person(name="P")
    production = Production(media_type="movie", tmdb_id=1, title="M")
    db_session.add_all([person, production])
    await db_session.commit()
    both = {"person_id": person.id, "production_id": production.id}

    for kwargs in ({}, both):
        db_session.add(Watch(user_id=alice_id, **kwargs))
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()


async def test_a_watch_must_belong_to_a_real_user(
    db_session: AsyncSession,
) -> None:
    person = Person(name="P")
    db_session.add(person)
    await db_session.commit()

    db_session.add(Watch(user_id=uuid.uuid4(), person_id=person.id))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_deleting_a_user_or_a_person_removes_their_watches(
    service: WatchService, db_session: AsyncSession, users: tuple[User, User]
) -> None:
    alice, bob = users
    await service.watch_person(alice.id, EDDIE)
    await service.watch_production(alice.id, WAREHOUSE)
    await service.watch_production(bob.id, SKYFALL)
    person = await db_session.scalar(select(Person))
    assert person is not None

    await db_session.delete(person)
    await db_session.commit()
    assert await _count(db_session, Watch) == 2

    await db_session.delete(alice)
    await db_session.commit()
    remaining = await db_session.scalars(select(Watch))
    assert [w.user_id for w in remaining] == [bob.id]


async def test_a_conflict_with_nothing_behind_it_is_not_swallowed(
    service: WatchService,
    db_session: AsyncSession,
    users: tuple[User, User],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(*_args: Any, **_kwargs: Any) -> None:
        raise IntegrityError("INSERT", {}, Exception("some other constraint"))

    monkeypatch.setattr(db_session, "commit", fail)

    with pytest.raises(IntegrityError):
        await service.watch_person(users[0].id, EDDIE)
