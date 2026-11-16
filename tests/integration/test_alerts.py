import uuid
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from casttrail.auth.service import AuthService
from casttrail.catalogue.models import Person
from casttrail.discovery.models import Appearance, AppearanceSource, Convention
from casttrail.identity.models import ExternalIdentity
from casttrail.matching.service import MatchService
from casttrail.notifications.models import (
    NotificationEvent,
    UserNotification,
    event_key_for,
)
from casttrail.notifications.service import RaisedAlerts, raise_alerts
from casttrail.providers.base import PersonDetail
from casttrail.watching.service import WatchService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from time_machine import TimeMachineFixture

    from casttrail.auth.models import User
    from casttrail.watching.models import Watch

pytestmark = pytest.mark.integration

START = datetime(2026, 11, 16, 12, tzinfo=UTC)
SOON = date(2026, 12, 5)
TODAY = date(2026, 11, 16)


@pytest.fixture(autouse=True)
def _start_the_clock(time_machine: TimeMachineFixture) -> None:
    time_machine.move_to(START, tick=False)


async def _user(session: AsyncSession, email: str) -> User:
    auth = AuthService(session, session_ttl=timedelta(hours=1))
    return await auth.create_user(
        email=email, display_name=email, password="correct horse battery"
    )


async def _watch(session: AsyncSession, user: User, name: str = "Paul McGann") -> Watch:
    return await WatchService(session).watch_person(
        user.id, PersonDetail(tmdb_id=1, name=name)
    )


async def _person(session: AsyncSession, watch: Watch) -> Person:
    person = await session.get(Person, watch.person_id)
    assert person is not None
    return person


async def _convention(
    session: AsyncSession, start: date | None = SOON, name: str = "Con"
) -> Convention:
    convention = Convention(
        name=name, start_date=start, end_date=start, country="united-kingdom"
    )
    session.add(convention)
    await session.commit()
    return convention


async def _guest(
    session: AsyncSession,
    convention: Convention,
    person: Person,
    *,
    linked: bool = True,
    provider: str = "rostercon",
) -> Appearance:
    """List the person at the convention, linked to them (confirmed) or not."""
    identity = ExternalIdentity(
        provider=provider,
        external_id=str(uuid.uuid4()),
        display_name=person.name,
        person_id=person.id if linked else None,
    )
    session.add(identity)
    await session.flush()
    appearance = Appearance(
        convention_id=convention.id, external_identity_id=identity.id
    )
    session.add(appearance)
    await session.flush()
    session.add(
        AppearanceSource(
            appearance_id=appearance.id, provider=provider, source_url="https://x.test"
        )
    )
    await session.commit()
    return appearance


async def _rematch(session: AsyncSession) -> None:
    await MatchService(session).rematch()


async def _alerts(session: AsyncSession, user: User) -> list[UserNotification]:
    return list(
        await session.scalars(
            select(UserNotification)
            .where(UserNotification.user_id == user.id)
            .order_by(UserNotification.created_at, UserNotification.id)
        )
    )


async def _event(session: AsyncSession, alert: UserNotification) -> NotificationEvent:
    event = await session.get(NotificationEvent, alert.event_id)
    assert event is not None
    return event


def _later(time_machine: TimeMachineFixture, minutes: int = 1) -> None:
    time_machine.shift(timedelta(minutes=minutes))


@pytest.fixture
async def user(db_session: AsyncSession) -> User:
    return await _user(db_session, "a@example.com")


# --- what makes an alert ----------------------------------------------------------


async def test_a_guest_found_after_watching_alerts_once(
    db_session: AsyncSession, user: User, time_machine: TimeMachineFixture
) -> None:
    person = await _person(db_session, await _watch(db_session, user))
    _later(time_machine)
    convention = await _convention(db_session)
    await _guest(db_session, convention, person)

    await _rematch(db_session)
    await _rematch(db_session)

    [alert] = await _alerts(db_session, user)
    event = await _event(db_session, alert)
    assert event.event_key == event_key_for(convention.id, person.id)
    assert (alert.rev, alert.read_at, alert.raised_at) == (1, None, alert.created_at)
    assert (event.start_date, event.end_date, event.cancelled) == (SOON, SOON, False)


async def test_what_was_known_before_watching_never_alerts(
    db_session: AsyncSession, user: User, time_machine: TimeMachineFixture
) -> None:
    other = await _user(db_session, "b@example.com")
    person = await _person(db_session, await _watch(db_session, other))
    _later(time_machine)
    await _guest(db_session, await _convention(db_session), person)
    _later(time_machine)
    await _watch(db_session, user)

    await _rematch(db_session)

    assert len(await _alerts(db_session, other)) == 1
    assert await _alerts(db_session, user) == []


async def test_a_guest_seen_at_the_moment_of_watching_counts_as_new(
    db_session: AsyncSession, user: User
) -> None:
    person = await _person(db_session, await _watch(db_session, user))
    await _guest(db_session, await _convention(db_session), person)

    await _rematch(db_session)

    assert len(await _alerts(db_session, user)) == 1


async def test_a_possible_match_waits_until_it_is_confirmed(
    db_session: AsyncSession, user: User, time_machine: TimeMachineFixture
) -> None:
    person = await _person(db_session, await _watch(db_session, user))
    _later(time_machine)
    appearance = await _guest(
        db_session, await _convention(db_session), person, linked=False
    )

    await _rematch(db_session)
    assert await _alerts(db_session, user) == []

    identity = await db_session.get(ExternalIdentity, appearance.external_identity_id)
    assert identity is not None
    identity.person_id = person.id
    await db_session.commit()
    await _rematch(db_session)

    assert len(await _alerts(db_session, user)) == 1


@pytest.mark.parametrize(
    "start",
    [None, date(2026, 11, 15), date(2026, 1, 1)],
    ids=["no date", "yesterday", "long ago"],
)
async def test_only_a_convention_that_starts_today_or_later_alerts(
    db_session: AsyncSession,
    user: User,
    time_machine: TimeMachineFixture,
    start: date | None,
) -> None:
    person = await _person(db_session, await _watch(db_session, user))
    _later(time_machine)
    await _guest(db_session, await _convention(db_session, start), person)
    await _guest(db_session, await _convention(db_session, TODAY, "Today"), person)

    await _rematch(db_session)

    [alert] = await _alerts(db_session, user)
    assert (await _event(db_session, alert)).start_date == TODAY


async def test_two_sources_listing_one_person_at_one_event_make_one_alert(
    db_session: AsyncSession, user: User, time_machine: TimeMachineFixture
) -> None:
    person = await _person(db_session, await _watch(db_session, user))
    _later(time_machine)
    convention = await _convention(db_session)
    await _guest(db_session, convention, person)
    await _guest(db_session, convention, person, provider="comiconomicon")

    await _rematch(db_session)

    assert len(await _alerts(db_session, user)) == 1
    assert await db_session.scalar(select(func.count(NotificationEvent.id))) == 1


async def test_each_user_has_their_own_alert_about_the_same_event(
    db_session: AsyncSession, user: User, time_machine: TimeMachineFixture
) -> None:
    other = await _user(db_session, "b@example.com")
    person = await _person(db_session, await _watch(db_session, user))
    await _watch(db_session, other)
    _later(time_machine)
    await _guest(db_session, await _convention(db_session), person)

    await _rematch(db_session)

    [mine] = await _alerts(db_session, user)
    [theirs] = await _alerts(db_session, other)
    assert mine.event_id == theirs.event_id
    assert mine.id != theirs.id


async def test_unwatching_keeps_the_alert_already_made(
    db_session: AsyncSession, user: User, time_machine: TimeMachineFixture
) -> None:
    watch = await _watch(db_session, user)
    _later(time_machine)
    await _guest(
        db_session, await _convention(db_session), await _person(db_session, watch)
    )
    await _rematch(db_session)

    assert await WatchService(db_session).unwatch(user.id, watch.id)
    await _rematch(db_session)

    assert len(await _alerts(db_session, user)) == 1


async def test_a_cancelled_event_never_makes_a_new_alert(
    db_session: AsyncSession, user: User, time_machine: TimeMachineFixture
) -> None:
    person = await _person(db_session, await _watch(db_session, user))
    _later(time_machine)
    convention = await _convention(db_session)
    convention.status = "cancelled"
    await _guest(db_session, convention, person)

    await _rematch(db_session)

    assert await _alerts(db_session, user) == []
    assert await db_session.scalar(select(func.count(NotificationEvent.id))) == 0


# --- raising an alert again -------------------------------------------------------


async def _read_alert(
    session: AsyncSession, user: User, time_machine: TimeMachineFixture
) -> tuple[Convention, UserNotification]:
    person = await _person(session, await _watch(session, user))
    _later(time_machine)
    convention = await _convention(session)
    await _guest(session, convention, person)
    await _rematch(session)
    [alert] = await _alerts(session, user)
    alert.read_at = START
    await session.commit()
    _later(time_machine, 60)
    return convention, alert


async def test_new_dates_raise_the_alert_again_as_unread(
    db_session: AsyncSession, user: User, time_machine: TimeMachineFixture
) -> None:
    convention, alert = await _read_alert(db_session, user, time_machine)

    convention.end_date = date(2026, 12, 6)
    await db_session.commit()
    await _rematch(db_session)
    await _rematch(db_session)

    await db_session.refresh(alert)
    event = await _event(db_session, alert)
    assert (alert.rev, event.rev, alert.read_at) == (2, 2, None)
    assert alert.raised_at > alert.created_at
    assert event.end_date == date(2026, 12, 6)


async def test_nothing_that_matters_leaves_a_read_alert_read(
    db_session: AsyncSession, user: User, time_machine: TimeMachineFixture
) -> None:
    convention, alert = await _read_alert(db_session, user, time_machine)

    convention.name = "Renamed"
    await db_session.commit()
    await _rematch(db_session)

    await db_session.refresh(alert)
    assert (alert.rev, alert.read_at) == (1, START)


async def test_a_cancelled_convention_raises_the_alert_again(
    db_session: AsyncSession, user: User, time_machine: TimeMachineFixture
) -> None:
    convention, alert = await _read_alert(db_session, user, time_machine)

    convention.status = "cancelled"
    await db_session.commit()
    await _rematch(db_session)

    await db_session.refresh(alert)
    assert (alert.rev, alert.read_at) == (2, None)
    assert (await _event(db_session, alert)).cancelled


async def test_a_guest_the_source_says_cancelled_raises_the_alert_again(
    db_session: AsyncSession, user: User, time_machine: TimeMachineFixture
) -> None:
    _, alert = await _read_alert(db_session, user, time_machine)

    line = await db_session.scalar(select(AppearanceSource))
    assert line is not None
    line.status = "cancelled"
    await db_session.commit()
    await _rematch(db_session)

    await db_session.refresh(alert)
    assert (alert.rev, alert.read_at) == (2, None)


async def test_raise_alerts_reports_what_it_did(
    db_session: AsyncSession, user: User, time_machine: TimeMachineFixture
) -> None:
    assert await raise_alerts(db_session) == RaisedAlerts(new=0, raised_again=0)
    convention, _ = await _read_alert(db_session, user, time_machine)
    convention.start_date = date(2026, 12, 4)
    await db_session.commit()

    assert await raise_alerts(db_session) == RaisedAlerts(new=0, raised_again=1)


async def test_a_user_without_an_alert_gets_none_once_the_event_is_cancelled(
    db_session: AsyncSession, user: User, time_machine: TimeMachineFixture
) -> None:
    other = await _user(db_session, "b@example.com")
    person = await _person(db_session, await _watch(db_session, user))
    await _watch(db_session, other)
    _later(time_machine)
    convention = await _convention(db_session)
    await _guest(db_session, convention, person)
    await _rematch(db_session)
    # The other user has no alert (gone with a since-removed watch, say) while the
    # event itself lives on for this user.
    [theirs] = await _alerts(db_session, other)
    await db_session.delete(theirs)
    convention.status = "cancelled"
    await db_session.commit()

    await _rematch(db_session)

    assert await _alerts(db_session, other) == []
    assert len(await _alerts(db_session, user)) == 1
