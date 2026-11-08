import uuid
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from casttrail.catalogue.models import Person
from casttrail.discovery.models import (
    Appearance,
    AppearanceSource,
    Convention,
    ConventionSource,
    SourceObservation,
)
from casttrail.identity.models import ExternalIdentity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


async def _convention(session: AsyncSession, name: str = "Some Con") -> Convention:
    convention = Convention(name=name)
    session.add(convention)
    await session.commit()
    return convention


async def _identity(
    session: AsyncSession, external_id: str = "paul-mcgann-en"
) -> ExternalIdentity:
    identity = ExternalIdentity(provider="rostercon", external_id=external_id)
    session.add(identity)
    await session.commit()
    return identity


# --- conventions ----------------------------------------------------------------


async def test_a_convention_needs_only_a_name(db_session: AsyncSession) -> None:
    convention = await _convention(db_session)

    stored = await db_session.get(Convention, convention.id)
    assert stored is not None
    assert (stored.start_date, stored.end_date, stored.status) == (None, None, None)
    assert stored.country is None
    assert stored.created_at.tzinfo is not None
    assert stored.updated_at.tzinfo is not None


async def test_a_convention_keeps_its_dates_and_place(db_session: AsyncSession) -> None:
    db_session.add(
        Convention(
            name="Follow The Yellow Gate Road",
            start_date=date(2027, 4, 17),
            end_date=date(2027, 4, 18),
            venue="Best Western Plus Bentley Hotel",
            city="Lincoln",
            region="England",
            country="united-kingdom",
            organizer="Mama Nox Presents",
            status="scheduled",
        )
    )
    await db_session.commit()

    stored = await db_session.scalar(select(Convention))
    assert stored is not None
    assert (stored.start_date, stored.end_date) == (
        date(2027, 4, 17),
        date(2027, 4, 18),
    )
    assert (stored.city, stored.country, stored.status) == (
        "Lincoln",
        "united-kingdom",
        "scheduled",
    )


@pytest.mark.parametrize("status", ["scheduled", "cancelled", "postponed", None])
async def test_the_known_statuses_are_accepted(
    db_session: AsyncSession, status: str | None
) -> None:
    db_session.add(Convention(name="C", status=status))

    await db_session.commit()


@pytest.mark.parametrize("status", ["", "Cancelled", "moved", "on"])
async def test_an_unknown_status_is_refused(
    db_session: AsyncSession, status: str
) -> None:
    db_session.add(Convention(name="C", status=status))

    with pytest.raises(IntegrityError):
        await db_session.commit()


# --- convention sources ---------------------------------------------------------


async def test_a_source_key_points_at_one_convention(db_session: AsyncSession) -> None:
    first = await _convention(db_session, "First")
    second = await _convention(db_session, "Second")
    db_session.add(
        ConventionSource(
            convention_id=first.id,
            provider="rostercon",
            source_key="some-con",
            source_url="https://www.rostercon.com/en/event-convention/some-con",
        )
    )
    await db_session.commit()

    db_session.add(
        ConventionSource(
            convention_id=second.id,
            provider="rostercon",
            source_key="some-con",
            source_url="https://www.rostercon.com/en/event-convention/some-con",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_one_convention_can_be_listed_by_two_sources(
    db_session: AsyncSession,
) -> None:
    convention = await _convention(db_session)
    db_session.add_all(
        [
            ConventionSource(
                convention_id=convention.id,
                provider=provider,
                source_key=key,
                source_url=f"https://example.test/{key}",
            )
            for provider, key in (("rostercon", "some-con"), ("comiconomicon", "42676"))
        ]
    )

    await db_session.commit()


async def test_the_same_key_is_fine_for_another_provider(
    db_session: AsyncSession,
) -> None:
    convention = await _convention(db_session)
    other = await _convention(db_session, "Other")
    for conv, provider in ((convention, "rostercon"), (other, "comiconomicon")):
        db_session.add(
            ConventionSource(
                convention_id=conv.id,
                provider=provider,
                source_key="1",
                source_url="https://example.test/1",
            )
        )

    await db_session.commit()


async def test_deleting_a_convention_removes_its_sources_and_appearances(
    db_session: AsyncSession,
) -> None:
    convention = await _convention(db_session)
    identity = await _identity(db_session)
    db_session.add_all(
        [
            ConventionSource(
                convention_id=convention.id,
                provider="rostercon",
                source_key="k",
                source_url="https://example.test/k",
            ),
            Appearance(convention_id=convention.id, external_identity_id=identity.id),
        ]
    )
    await db_session.commit()

    await db_session.delete(convention)
    await db_session.commit()

    assert await db_session.scalar(select(ConventionSource)) is None
    assert await db_session.scalar(select(Appearance)) is None
    assert await db_session.scalar(select(ExternalIdentity)) is not None


# --- observations ---------------------------------------------------------------


async def test_an_observation_records_a_hash_and_whether_it_was_complete(
    db_session: AsyncSession,
) -> None:
    db_session.add(
        SourceObservation(
            provider="rostercon",
            source_key="some-con",
            content_hash="a" * 64,
            complete=False,
            fetched_at=datetime(2026, 11, 8, 12, 0, tzinfo=UTC),
        )
    )
    await db_session.commit()

    stored = await db_session.scalar(select(SourceObservation))
    assert stored is not None
    assert (stored.content_hash, stored.complete) == ("a" * 64, False)
    assert stored.fetched_at == datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


async def test_observations_of_the_same_page_are_kept_side_by_side(
    db_session: AsyncSession,
) -> None:
    for hour in (10, 11, 12):
        db_session.add(
            SourceObservation(
                provider="rostercon",
                source_key="some-con",
                content_hash="b" * 64,
                complete=True,
                fetched_at=datetime(2026, 11, 8, hour, 0, tzinfo=UTC),
            )
        )

    await db_session.commit()

    assert len(list(await db_session.scalars(select(SourceObservation)))) == 3


# --- appearances ----------------------------------------------------------------


async def test_an_appearance_starts_unmatched_to_any_person(
    db_session: AsyncSession,
) -> None:
    convention = await _convention(db_session)
    identity = await _identity(db_session)

    db_session.add(
        Appearance(convention_id=convention.id, external_identity_id=identity.id)
    )
    await db_session.commit()

    stored = await db_session.scalar(select(Appearance))
    assert stored is not None
    assert stored.person_id is None


async def test_a_guest_appears_once_at_a_convention(db_session: AsyncSession) -> None:
    convention = await _convention(db_session)
    identity = await _identity(db_session)
    db_session.add(
        Appearance(convention_id=convention.id, external_identity_id=identity.id)
    )
    await db_session.commit()

    db_session.add(
        Appearance(convention_id=convention.id, external_identity_id=identity.id)
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_a_guest_can_appear_at_two_conventions(db_session: AsyncSession) -> None:
    identity = await _identity(db_session)
    for name in ("One", "Two"):
        convention = await _convention(db_session, name)
        db_session.add(
            Appearance(convention_id=convention.id, external_identity_id=identity.id)
        )

    await db_session.commit()


async def test_deleting_a_person_unmatches_the_appearance_but_keeps_it(
    db_session: AsyncSession,
) -> None:
    convention = await _convention(db_session)
    person = Person(name="Paul McGann")
    db_session.add(person)
    await db_session.commit()
    identity = await _identity(db_session)
    db_session.add(
        Appearance(
            convention_id=convention.id,
            external_identity_id=identity.id,
            person_id=person.id,
        )
    )
    await db_session.commit()

    await db_session.delete(person)
    await db_session.commit()

    stored = await db_session.scalar(select(Appearance))
    assert stored is not None
    assert stored.person_id is None


async def test_deleting_an_identity_removes_its_appearances(
    db_session: AsyncSession,
) -> None:
    convention = await _convention(db_session)
    identity = await _identity(db_session)
    db_session.add(
        Appearance(convention_id=convention.id, external_identity_id=identity.id)
    )
    await db_session.commit()

    await db_session.delete(identity)
    await db_session.commit()

    assert await db_session.scalar(select(Appearance)) is None


async def test_an_appearance_must_point_at_real_rows(db_session: AsyncSession) -> None:
    convention = await _convention(db_session)

    db_session.add(
        Appearance(convention_id=convention.id, external_identity_id=uuid.uuid4())
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


# --- appearance sources ---------------------------------------------------------


async def _appearance(db_session: AsyncSession) -> Appearance:
    convention = await _convention(db_session)
    identity = await _identity(db_session)
    appearance = Appearance(
        convention_id=convention.id, external_identity_id=identity.id
    )
    db_session.add(appearance)
    await db_session.commit()
    return appearance


async def test_a_new_source_line_is_listed_with_no_status(
    db_session: AsyncSession,
) -> None:
    appearance = await _appearance(db_session)

    db_session.add(
        AppearanceSource(
            appearance_id=appearance.id,
            provider="rostercon",
            source_url="https://www.rostercon.com/en/people/paul-mcgann-en",
        )
    )
    await db_session.commit()

    stored = await db_session.scalar(select(AppearanceSource))
    assert stored is not None
    assert stored.listed is True
    assert stored.status is None


async def test_no_longer_listed_is_a_flag_and_not_a_status(
    db_session: AsyncSession,
) -> None:
    appearance = await _appearance(db_session)
    db_session.add(
        AppearanceSource(
            appearance_id=appearance.id,
            provider="rostercon",
            source_url="https://example.test/x",
            listed=False,
        )
    )
    await db_session.commit()

    stored = await db_session.scalar(select(AppearanceSource))
    assert stored is not None
    assert (stored.listed, stored.status) == (False, None)


async def test_two_sources_can_list_one_appearance_but_not_one_twice(
    db_session: AsyncSession,
) -> None:
    appearance = await _appearance(db_session)
    for provider in ("rostercon", "comiconomicon"):
        db_session.add(
            AppearanceSource(
                appearance_id=appearance.id,
                provider=provider,
                source_url="https://example.test/x",
            )
        )
    await db_session.commit()

    db_session.add(
        AppearanceSource(
            appearance_id=appearance.id,
            provider="rostercon",
            source_url="https://example.test/y",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.parametrize("status", ["", "gone", "Cancelled"])
async def test_an_appearance_source_refuses_an_unknown_status(
    db_session: AsyncSession, status: str
) -> None:
    appearance = await _appearance(db_session)

    db_session.add(
        AppearanceSource(
            appearance_id=appearance.id,
            provider="rostercon",
            source_url="https://example.test/x",
            status=status,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_deleting_an_appearance_removes_its_source_lines(
    db_session: AsyncSession,
) -> None:
    appearance = await _appearance(db_session)
    db_session.add(
        AppearanceSource(
            appearance_id=appearance.id,
            provider="rostercon",
            source_url="https://example.test/x",
        )
    )
    await db_session.commit()

    await db_session.delete(appearance)
    await db_session.commit()

    assert await db_session.scalar(select(AppearanceSource)) is None
