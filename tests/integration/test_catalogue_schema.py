import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from casttrail.catalogue.models import Credit, Person, Production
from casttrail.identity.models import ExternalIdentity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _person(session: AsyncSession, name: str = "Eddie McClintock") -> Person:
    person = Person(name=name)
    session.add(person)
    await session.commit()
    return person


async def _production(
    session: AsyncSession, media_type: str = "tv", tmdb_id: int = 15819
) -> Production:
    production = Production(
        media_type=media_type, tmdb_id=tmdb_id, title="Warehouse 13", release_year=2009
    )
    session.add(production)
    await session.commit()
    return production


# --- people and productions -----------------------------------------------------


async def test_a_person_needs_only_a_name(db_session: AsyncSession) -> None:
    person = await _person(db_session)

    stored = await db_session.get(Person, person.id)
    assert stored is not None
    assert stored.known_for_department is None
    assert stored.profile_path is None
    assert stored.created_at.tzinfo is not None


async def test_the_same_tmdb_id_can_be_a_film_and_a_series(
    db_session: AsyncSession,
) -> None:
    await _production(db_session, "movie", 1234)
    await _production(db_session, "tv", 1234)

    found = await db_session.scalars(
        select(Production).where(Production.tmdb_id == 1234)
    )
    assert {p.media_type for p in found} == {"movie", "tv"}


async def test_a_production_is_unique_by_type_and_tmdb_id(
    db_session: AsyncSession,
) -> None:
    await _production(db_session, "tv", 15819)

    with pytest.raises(IntegrityError):
        await _production(db_session, "tv", 15819)


@pytest.mark.parametrize("bad", ["person", "", "TV", "series"])
async def test_a_production_must_be_a_film_or_a_series(
    db_session: AsyncSession, bad: str
) -> None:
    with pytest.raises(IntegrityError):
        await _production(db_session, bad, 1)


# --- credits --------------------------------------------------------------------


async def test_a_credit_links_a_person_to_a_production(
    db_session: AsyncSession,
) -> None:
    person = await _person(db_session)
    production = await _production(db_session)

    db_session.add(
        Credit(
            person_id=person.id,
            production_id=production.id,
            character="Pete Lattimer",
            billing_order=0,
        )
    )
    await db_session.commit()

    credit = await db_session.scalar(select(Credit))
    assert credit is not None
    assert (credit.character, credit.billing_order) == ("Pete Lattimer", 0)


async def test_billing_order_and_character_may_be_unknown(
    db_session: AsyncSession,
) -> None:
    person = await _person(db_session)
    production = await _production(db_session)

    db_session.add(Credit(person_id=person.id, production_id=production.id))
    await db_session.commit()

    credit = await db_session.scalar(select(Credit))
    assert credit is not None
    assert credit.character is None
    assert credit.billing_order is None


async def test_a_person_has_one_credit_per_production(db_session: AsyncSession) -> None:
    person = await _person(db_session)
    production = await _production(db_session)
    db_session.add(Credit(person_id=person.id, production_id=production.id))
    await db_session.commit()

    db_session.add(Credit(person_id=person.id, production_id=production.id))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_a_credit_must_point_at_real_rows(db_session: AsyncSession) -> None:
    production = await _production(db_session)

    db_session.add(Credit(person_id=uuid.uuid4(), production_id=production.id))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_deleting_a_person_or_a_production_removes_its_credits(
    db_session: AsyncSession,
) -> None:
    alice = await _person(db_session, "Alice")
    bob = await _person(db_session, "Bob")
    show = await _production(db_session, "tv", 1)
    film = await _production(db_session, "movie", 2)
    db_session.add_all(
        [
            Credit(person_id=alice.id, production_id=show.id),
            Credit(person_id=bob.id, production_id=show.id),
            Credit(person_id=bob.id, production_id=film.id),
        ]
    )
    await db_session.commit()

    await db_session.delete(alice)
    await db_session.commit()
    assert len(list(await db_session.scalars(select(Credit)))) == 2

    await db_session.delete(film)
    await db_session.commit()
    remaining = list(await db_session.scalars(select(Credit)))
    assert [(c.person_id, c.production_id) for c in remaining] == [(bob.id, show.id)]


# --- external identities --------------------------------------------------------


async def test_an_identity_can_exist_before_it_is_linked(
    db_session: AsyncSession,
) -> None:
    db_session.add(ExternalIdentity(provider="rostercon", external_id="paul-mcgann-en"))
    await db_session.commit()

    identity = await db_session.scalar(select(ExternalIdentity))
    assert identity is not None
    assert identity.person_id is None


async def test_one_person_can_hold_identities_from_several_providers(
    db_session: AsyncSession,
) -> None:
    person = await _person(db_session)
    db_session.add_all(
        [
            ExternalIdentity(
                provider="tmdb", external_id="120679", person_id=person.id
            ),
            ExternalIdentity(
                provider="imdb", external_id="nm0565858", person_id=person.id
            ),
        ]
    )
    await db_session.commit()

    found = await db_session.scalars(
        select(ExternalIdentity).where(ExternalIdentity.person_id == person.id)
    )
    assert {i.provider for i in found} == {"tmdb", "imdb"}


async def test_an_identity_is_unique_by_provider_and_external_id(
    db_session: AsyncSession,
) -> None:
    db_session.add(ExternalIdentity(provider="tmdb", external_id="1"))
    await db_session.commit()

    db_session.add(ExternalIdentity(provider="tmdb", external_id="1"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_the_same_external_id_is_fine_for_another_provider(
    db_session: AsyncSession,
) -> None:
    db_session.add_all(
        [
            ExternalIdentity(provider="tmdb", external_id="1"),
            ExternalIdentity(provider="comiconomicon", external_id="1"),
        ]
    )

    await db_session.commit()


async def test_deleting_a_person_unlinks_their_identities_but_keeps_them(
    db_session: AsyncSession,
) -> None:
    person = await _person(db_session)
    db_session.add(
        ExternalIdentity(provider="tmdb", external_id="120679", person_id=person.id)
    )
    await db_session.commit()

    await db_session.delete(person)
    await db_session.commit()

    identity = await db_session.scalar(select(ExternalIdentity))
    assert identity is not None
    assert identity.person_id is None


async def test_an_identity_cannot_point_at_a_missing_person(
    db_session: AsyncSession,
) -> None:
    db_session.add(
        ExternalIdentity(provider="tmdb", external_id="1", person_id=uuid.uuid4())
    )

    with pytest.raises(IntegrityError):
        await db_session.commit()
