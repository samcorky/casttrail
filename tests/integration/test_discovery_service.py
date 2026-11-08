from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select

from casttrail.catalogue.models import Person
from casttrail.discovery.models import (
    Appearance,
    AppearanceSource,
    Convention,
    ConventionSource,
    SourceObservation,
)
from casttrail.discovery.service import DiscoveryService, content_hash
from casttrail.identity.models import ExternalIdentity
from casttrail.providers.base import ConventionInfo, GuestObservation, ListingResult
from casttrail.providers.rostercon import parse_event_page

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[1] / "fixtures" / "rostercon"
START = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


class Clock:
    """A clock that moves forward a minute each time it is read."""

    def __init__(self) -> None:
        self.current = START

    def __call__(self) -> datetime:
        self.current += timedelta(minutes=1)
        return self.current


def _convention(**changes: Any) -> ConventionInfo:
    values: dict[str, Any] = {
        "source_key": "some-con",
        "name": "Some Con",
        "source_url": "https://www.rostercon.com/en/event-convention/some-con",
        "start_date": date(2027, 4, 17),
        "end_date": date(2027, 4, 18),
        "venue": "A Hotel",
        "city": "Lincoln",
        "region": "England",
        "country": "united-kingdom",
        "organizer": "An Organiser",
        "status": "scheduled",
    }
    return ConventionInfo(**(values | changes))


def _guest(key: str, name: str | None = None, **changes: Any) -> GuestObservation:
    return GuestObservation(
        source_key=key,
        name=name or key.replace("-", " ").title(),
        source_url=f"https://www.rostercon.com/en/people/{key}",
        **changes,
    )


def _listing(*keys: str, complete: bool = True) -> ListingResult:
    return ListingResult(items=[_guest(k) for k in keys], complete=complete)


async def _count(session: AsyncSession, model: Any) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


@pytest.fixture
def service(db_session: AsyncSession) -> DiscoveryService:
    return DiscoveryService(db_session, now=Clock())


async def _lines(session: AsyncSession) -> dict[str, AppearanceSource]:
    """Each appearance's source line, keyed by the guest's source key."""
    rows = await session.execute(
        select(ExternalIdentity.external_id, AppearanceSource)
        .join(Appearance, Appearance.external_identity_id == ExternalIdentity.id)
        .join(AppearanceSource, AppearanceSource.appearance_id == Appearance.id)
    )
    return {row[0]: row[1] for row in rows}


# --- the first ingest -----------------------------------------------------------


async def test_a_new_page_creates_the_convention_its_source_and_its_guests(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    result = await service.ingest(
        "rostercon", _convention(), _listing("alexis-cruz", "andrew-jackson")
    )

    convention = await db_session.scalar(select(Convention))
    assert convention is not None
    assert (convention.name, convention.city, convention.status) == (
        "Some Con",
        "Lincoln",
        "scheduled",
    )
    assert (convention.start_date, convention.end_date) == (
        date(2027, 4, 17),
        date(2027, 4, 18),
    )
    source = await db_session.scalar(select(ConventionSource))
    assert source is not None
    assert (source.provider, source.source_key, source.convention_id) == (
        "rostercon",
        "some-con",
        convention.id,
    )
    assert source.source_url == "https://www.rostercon.com/en/event-convention/some-con"
    assert result.convention_id == convention.id
    assert (result.convention_created, result.guests_added) == (True, 2)
    assert result.guests_unlisted == 0
    assert result.changed is True


async def test_each_guest_becomes_an_unmatched_identity_with_a_name(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest(
        "rostercon",
        _convention(),
        ListingResult(items=[_guest("paul-mcgann-en", "Paul McGann")], complete=True),
    )

    identity = await db_session.scalar(select(ExternalIdentity))
    assert identity is not None
    assert (identity.provider, identity.external_id) == ("rostercon", "paul-mcgann-en")
    assert identity.display_name == "Paul McGann"
    assert identity.person_id is None
    appearance = await db_session.scalar(select(Appearance))
    assert appearance is not None
    assert appearance.external_identity_id == identity.id
    assert appearance.person_id is None


async def test_each_appearance_records_where_it_was_seen(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(), _listing("alexis-cruz"))

    line = (await _lines(db_session))["alexis-cruz"]
    assert line.provider == "rostercon"
    assert line.source_url == "https://www.rostercon.com/en/people/alexis-cruz"
    assert line.listed is True
    assert line.status is None
    assert line.first_seen_at == line.last_seen_at


# --- idempotence ----------------------------------------------------------------


async def test_ingesting_the_same_page_twice_changes_nothing_but_the_clock(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    first = await service.ingest(
        "rostercon", _convention(), _listing("alexis-cruz", "andrew-jackson")
    )
    before = await _lines(db_session)
    first_seen = {k: v.first_seen_at for k, v in before.items()}
    seen_before = {k: v.last_seen_at for k, v in before.items()}

    second = await service.ingest(
        "rostercon", _convention(), _listing("alexis-cruz", "andrew-jackson")
    )

    assert second.convention_id == first.convention_id
    assert (second.convention_created, second.guests_added) == (False, 0)
    assert second.changed is False
    for model, expected in (
        (Convention, 1),
        (ConventionSource, 1),
        (ExternalIdentity, 2),
        (Appearance, 2),
        (AppearanceSource, 2),
    ):
        assert await _count(db_session, model) == expected, model.__name__
    after = await _lines(db_session)
    assert {k: v.first_seen_at for k, v in after.items()} == first_seen
    assert all(after[k].last_seen_at > seen_before[k] for k in after)


async def test_a_second_run_does_not_touch_a_convention_that_did_not_change(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(), _listing("a-guest"))
    convention = await db_session.scalar(select(Convention))
    assert convention is not None
    stamp = convention.updated_at

    await service.ingest("rostercon", _convention(), _listing("a-guest"))

    await db_session.refresh(convention)
    assert convention.updated_at == stamp


async def test_the_order_of_the_guests_does_not_matter(
    service: DiscoveryService,
) -> None:
    one = await service.ingest(
        "rostercon", _convention(), _listing("a-a", "b-b", "c-c")
    )
    two = await service.ingest(
        "rostercon", _convention(), _listing("c-c", "a-a", "b-b")
    )

    assert one.changed is True
    assert two.changed is False


# --- updating what is known -----------------------------------------------------


async def test_new_details_from_the_source_replace_old_ones(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(), _listing())
    convention = await db_session.scalar(select(Convention))
    assert convention is not None
    stamp = convention.updated_at

    result = await service.ingest(
        "rostercon",
        _convention(
            name="Some Con 2", venue="Another Hotel", end_date=date(2027, 4, 19)
        ),
        _listing(),
    )

    await db_session.refresh(convention)
    assert (convention.name, convention.venue) == ("Some Con 2", "Another Hotel")
    assert convention.end_date == date(2027, 4, 19)
    assert convention.updated_at > stamp
    assert result.changed is True
    assert await _count(db_session, Convention) == 1


async def test_a_detail_the_source_leaves_out_is_never_cleared(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(), _listing())

    await service.ingest(
        "rostercon",
        _convention(
            venue=None, city=None, status=None, start_date=None, organizer=None
        ),
        _listing(),
    )

    convention = await db_session.scalar(select(Convention))
    assert convention is not None
    assert (convention.venue, convention.city) == ("A Hotel", "Lincoln")
    assert convention.status == "scheduled"
    assert convention.start_date == date(2027, 4, 17)
    assert convention.organizer == "An Organiser"


async def test_only_a_stated_cancellation_cancels_an_event(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(), _listing("a-guest"))
    await service.ingest(
        "rostercon", _convention(status=None), _listing(complete=False)
    )
    convention = await db_session.scalar(select(Convention))
    assert convention is not None
    assert convention.status == "scheduled"

    await service.ingest(
        "rostercon", _convention(status="cancelled"), _listing("a-guest")
    )

    await db_session.refresh(convention)
    assert convention.status == "cancelled"


async def test_a_source_can_take_a_cancellation_back(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(status="cancelled"), _listing())

    await service.ingest("rostercon", _convention(status="scheduled"), _listing())

    convention = await db_session.scalar(select(Convention))
    assert convention is not None
    assert convention.status == "scheduled"


# --- guests coming and going ----------------------------------------------------


async def test_a_new_guest_is_added_to_an_existing_convention(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(), _listing("a-a"))

    result = await service.ingest("rostercon", _convention(), _listing("a-a", "b-b"))

    assert result.guests_added == 1
    assert set(await _lines(db_session)) == {"a-a", "b-b"}
    assert await _count(db_session, Convention) == 1


async def test_a_guest_missing_from_a_complete_listing_is_unlisted_not_removed(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(), _listing("a-a", "b-b"))

    result = await service.ingest("rostercon", _convention(), _listing("a-a"))

    lines = await _lines(db_session)
    assert result.guests_unlisted == 1
    assert (lines["a-a"].listed, lines["b-b"].listed) == (True, False)
    assert lines["b-b"].status is None  # never cancelled by going missing
    assert await _count(db_session, Appearance) == 2
    assert await _count(db_session, ExternalIdentity) == 2


async def test_an_incomplete_listing_unlists_nobody(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(), _listing("a-a", "b-b"))

    result = await service.ingest(
        "rostercon", _convention(), _listing("a-a", complete=False)
    )

    lines = await _lines(db_session)
    assert result.guests_unlisted == 0
    assert (lines["a-a"].listed, lines["b-b"].listed) == (True, True)


async def test_an_empty_incomplete_listing_keeps_every_guest(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(), _listing("a-a", "b-b"))

    await service.ingest("rostercon", _convention(), _listing(complete=False))

    assert all(line.listed for line in (await _lines(db_session)).values())


async def test_an_empty_complete_listing_unlists_everyone(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(), _listing("a-a", "b-b"))

    result = await service.ingest("rostercon", _convention(), _listing())

    assert result.guests_unlisted == 2
    assert not any(line.listed for line in (await _lines(db_session)).values())


async def test_a_guest_who_comes_back_is_listed_again_without_a_duplicate(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(), _listing("a-a", "b-b"))
    await service.ingest("rostercon", _convention(), _listing("a-a"))

    result = await service.ingest("rostercon", _convention(), _listing("a-a", "b-b"))

    assert result.guests_added == 0
    assert (await _lines(db_session))["b-b"].listed is True
    assert await _count(db_session, Appearance) == 2


async def test_unlisting_does_not_repeat_for_a_guest_already_unlisted(
    service: DiscoveryService,
) -> None:
    await service.ingest("rostercon", _convention(), _listing("a-a", "b-b"))
    await service.ingest("rostercon", _convention(), _listing("a-a"))

    again = await service.ingest("rostercon", _convention(), _listing("a-a"))

    assert again.guests_unlisted == 0


async def test_a_guests_stated_status_is_recorded_and_not_cleared_by_silence(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    cancelled = ListingResult(items=[_guest("a-a", status="cancelled")], complete=True)
    await service.ingest("rostercon", _convention(), cancelled)
    assert (await _lines(db_session))["a-a"].status == "cancelled"

    await service.ingest("rostercon", _convention(), _listing("a-a"))

    assert (await _lines(db_session))["a-a"].status == "cancelled"


async def test_a_status_the_source_states_later_is_recorded(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(), _listing("a-a"))
    assert (await _lines(db_session))["a-a"].status is None

    stated = ListingResult(items=[_guest("a-a", status="cancelled")], complete=True)
    await service.ingest("rostercon", _convention(), stated)

    assert (await _lines(db_session))["a-a"].status == "cancelled"


# --- identities and people are protected ----------------------------------------


async def test_a_link_made_by_hand_survives_every_sync(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(), _listing("paul-mcgann-en"))
    person = Person(name="Paul McGann")
    db_session.add(person)
    await db_session.flush()
    identity = await db_session.scalar(select(ExternalIdentity))
    appearance = await db_session.scalar(select(Appearance))
    assert identity is not None
    assert appearance is not None
    identity.person_id = person.id
    appearance.person_id = person.id
    await db_session.commit()

    for _ in range(3):
        await service.ingest("rostercon", _convention(), _listing("paul-mcgann-en"))
    renamed = ListingResult(
        items=[_guest("paul-mcgann-en", "Paul McGann (corrected)")], complete=True
    )
    await service.ingest("rostercon", _convention(), renamed)
    await service.ingest("rostercon", _convention(), _listing())

    await db_session.refresh(identity)
    await db_session.refresh(appearance)
    assert identity.person_id == person.id
    assert appearance.person_id == person.id
    assert identity.display_name == "Paul McGann (corrected)"


async def test_the_source_can_correct_a_guests_displayed_name(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest(
        "rostercon",
        _convention(),
        ListingResult(items=[_guest("a-a", "Old Name")], complete=True),
    )

    await service.ingest(
        "rostercon",
        _convention(),
        ListingResult(items=[_guest("a-a", "New Name")], complete=True),
    )

    identity = await db_session.scalar(select(ExternalIdentity))
    assert identity is not None
    assert identity.display_name == "New Name"
    assert await _count(db_session, ExternalIdentity) == 1


async def test_one_guest_at_two_conventions_is_one_identity_and_two_appearances(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(), _listing("shared"))
    await service.ingest(
        "rostercon",
        _convention(source_key="other-con", name="Other"),
        _listing("shared"),
    )

    assert await _count(db_session, ExternalIdentity) == 1
    assert await _count(db_session, Appearance) == 2
    assert await _count(db_session, Convention) == 2


# --- more than one source -------------------------------------------------------


async def test_the_same_key_from_two_providers_is_two_separate_conventions(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(source_key="1"), _listing("g"))
    await service.ingest("comiconomicon", _convention(source_key="1"), _listing("g"))

    assert await _count(db_session, Convention) == 2
    assert await _count(db_session, ConventionSource) == 2
    assert await _count(db_session, ExternalIdentity) == 2


async def test_one_sources_complete_listing_does_not_unlist_another_sources_line(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    result = await service.ingest("rostercon", _convention(), _listing("g"))
    appearance = await db_session.scalar(select(Appearance))
    assert appearance is not None
    db_session.add(
        AppearanceSource(
            appearance_id=appearance.id,
            provider="comiconomicon",
            source_url="https://example.test/g",
            listed=True,
        )
    )
    await db_session.commit()

    await service.ingest("rostercon", _convention(), _listing())

    rows = await db_session.execute(
        select(AppearanceSource.provider, AppearanceSource.listed)
    )
    assert dict(rows.all()) == {"rostercon": False, "comiconomicon": True}
    assert result.convention_id is not None


# --- observations ---------------------------------------------------------------


async def test_every_ingest_records_what_was_seen_and_whether_it_was_whole(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(), _listing("a-a", complete=False))

    observation = await db_session.scalar(select(SourceObservation))
    assert observation is not None
    assert (observation.provider, observation.source_key) == ("rostercon", "some-con")
    assert observation.complete is False
    assert len(observation.content_hash) == 64


async def test_only_the_latest_observations_of_a_page_are_kept(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    for number in range(6):
        await service.ingest(
            "rostercon", _convention(name=f"Name {number}"), _listing()
        )

    rows = list(
        await db_session.scalars(
            select(SourceObservation).order_by(SourceObservation.fetched_at)
        )
    )
    assert len(rows) == 3  # the plan: prune to the last 3 per page
    assert [r.fetched_at for r in rows] == sorted(r.fetched_at for r in rows)
    newest = await db_session.scalar(select(func.max(SourceObservation.fetched_at)))
    assert rows[-1].fetched_at == newest


async def test_pruning_one_page_leaves_other_pages_alone(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    await service.ingest("rostercon", _convention(source_key="other"), _listing())
    for number in range(5):
        await service.ingest(
            "rostercon", _convention(name=f"Name {number}"), _listing()
        )

    others = await db_session.scalar(
        select(func.count())
        .select_from(SourceObservation)
        .where(SourceObservation.source_key == "other")
    )
    assert others == 1


def test_the_hash_ignores_guest_order_and_the_etag_but_not_content() -> None:
    base = content_hash(_convention(), _listing("a-a", "b-b"))

    assert content_hash(_convention(), _listing("b-b", "a-a")) == base
    assert (
        content_hash(
            _convention(),
            ListingResult(
                items=_listing("a-a", "b-b").items, complete=True, etag='"x"'
            ),
        )
        == base
    )
    assert content_hash(_convention(name="Changed"), _listing("a-a", "b-b")) != base
    assert content_hash(_convention(), _listing("a-a")) != base
    assert content_hash(_convention(), _listing("a-a", "b-b", complete=False)) != base
    assert len(base) == 64


# --- all or nothing -------------------------------------------------------------


async def test_a_failure_part_way_leaves_nothing_behind(
    service: DiscoveryService,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*_args: Any, **_kwargs: Any) -> None:
        msg = "disk on fire"
        raise RuntimeError(msg)

    monkeypatch.setattr(service, "_prune", boom)

    with pytest.raises(RuntimeError, match="disk on fire"):
        await service.ingest("rostercon", _convention(), _listing("a-a"))

    for model in (
        Convention,
        ConventionSource,
        ExternalIdentity,
        Appearance,
        AppearanceSource,
        SourceObservation,
    ):
        assert await _count(db_session, model) == 0, model.__name__


# --- the real pages -------------------------------------------------------------


async def test_real_rostercon_pages_end_to_end(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    lincoln = parse_event_page(
        (FIXTURES / "event_follow-the-yellow-gate-road-take-4.html").read_text(
            encoding="utf-8"
        )
    )
    assert lincoln.convention is not None

    first = await service.ingest("rostercon", lincoln.convention, lincoln.guests)
    again = await service.ingest("rostercon", lincoln.convention, lincoln.guests)

    assert (first.guests_added, again.guests_added) == (3, 0)
    assert again.changed is False
    convention = await db_session.scalar(select(Convention))
    assert convention is not None
    assert (convention.city, convention.country, convention.organizer) == (
        "Lincoln",
        "united-kingdom",
        "Mama Nox Presents",
    )
    names = await db_session.scalars(
        select(ExternalIdentity.display_name).order_by(ExternalIdentity.display_name)
    )
    assert list(names) == ["Alexis Cruz", "Andrew Jackson", "Michael Shanks"]


async def test_a_real_cancelled_event_is_recorded_as_cancelled(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    star = parse_event_page(
        (FIXTURES / "event_star-con-2026_cancelled.html").read_text(encoding="utf-8")
    )
    assert star.convention is not None

    await service.ingest("rostercon", star.convention, star.guests)

    convention = await db_session.scalar(select(Convention))
    assert convention is not None
    assert convention.status == "cancelled"
    assert await _count(db_session, Appearance) == 2


async def test_a_real_page_that_changed_shape_cannot_remove_known_guests(
    service: DiscoveryService, db_session: AsyncSession
) -> None:
    lincoln = parse_event_page(
        (FIXTURES / "event_follow-the-yellow-gate-road-take-4.html").read_text(
            encoding="utf-8"
        )
    )
    assert lincoln.convention is not None
    await service.ingest("rostercon", lincoln.convention, lincoln.guests)
    broken = ListingResult(items=[], complete=False)

    result = await service.ingest("rostercon", lincoln.convention, broken)

    assert result.guests_unlisted == 0
    assert all(line.listed for line in (await _lines(db_session)).values())
