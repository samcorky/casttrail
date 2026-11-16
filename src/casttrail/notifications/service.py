import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import case, func, select

from casttrail.db import utc_now
from casttrail.discovery.models import Appearance, AppearanceSource, Convention
from casttrail.matching.models import Match
from casttrail.notifications.models import (
    NotificationEvent,
    UserNotification,
    event_key_for,
)
from casttrail.watching.models import Watch

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RaisedAlerts:
    """What bringing the alerts up to date did."""

    new: int
    raised_again: int


@dataclass(frozen=True)
class _News:
    user_id: uuid.UUID
    convention_id: uuid.UUID
    person_id: uuid.UUID
    start_date: date | None
    end_date: date | None
    cancelled: bool


async def raise_alerts(session: AsyncSession) -> RaisedAlerts:
    """Make or raise again each user's alerts from their confirmed matches.

    Runs inside the caller's transaction, after the matches are up to date. Only a
    confirmed match to a convention that starts today or later counts, and only if
    the person's appearance there was first seen after the user started watching
    (``Watch.alerts_from``): following someone never alerts about what was already
    known. An alert already made is raised again, and marked unread, only when its
    dates change or it is cancelled. A cancelled event never makes a new alert. Alerts
    are never removed here, so running it again changes nothing.

    Args:
        session: The session the rematch is working in.

    Returns:
        How many alerts were made and raised again.
    """
    now = utc_now()
    news = await _news(session, now.date())
    if not news:
        return RaisedAlerts(new=0, raised_again=0)
    keys = {event_key_for(n.convention_id, n.person_id) for n in news}
    events = {
        event.event_key: event
        for event in await session.scalars(
            select(NotificationEvent).where(NotificationEvent.event_key.in_(keys))
        )
    }
    alerts = {
        (alert.user_id, alert.event_id): alert
        for alert in await session.scalars(
            select(UserNotification).where(
                UserNotification.event_id.in_([e.id for e in events.values()])
            )
        )
    }
    new = raised_again = 0
    for item in news:
        event = _event(session, events, item, now)
        if event is None:
            continue
        alert = alerts.get((item.user_id, event.id))
        if alert is None:
            if item.cancelled:
                continue
            session.add(
                UserNotification(
                    user_id=item.user_id,
                    event_id=event.id,
                    rev=event.rev,
                    created_at=now,
                    raised_at=now,
                )
            )
            new += 1
        elif alert.rev < event.rev:
            alert.rev = event.rev
            alert.raised_at = now
            alert.read_at = None
            raised_again += 1
    if new or raised_again:
        logger.info("alerts_raised", new=new, raised_again=raised_again)
    return RaisedAlerts(new=new, raised_again=raised_again)


def _event(
    session: AsyncSession,
    events: dict[str, NotificationEvent],
    item: _News,
    now: datetime,
) -> NotificationEvent | None:
    """Find the event for some news, making it or moving it on as needed."""
    key = event_key_for(item.convention_id, item.person_id)
    event = events.get(key)
    if event is None:
        if item.cancelled:
            return None
        event = NotificationEvent(
            id=uuid.uuid4(),
            event_key=key,
            convention_id=item.convention_id,
            person_id=item.person_id,
            rev=1,
            start_date=item.start_date,
            end_date=item.end_date,
            cancelled=False,
            created_at=now,
            updated_at=now,
        )
        session.add(event)
        events[key] = event
    elif (event.start_date, event.end_date, event.cancelled) != (
        item.start_date,
        item.end_date,
        item.cancelled,
    ):
        event.rev += 1
        event.start_date = item.start_date
        event.end_date = item.end_date
        event.cancelled = item.cancelled
        event.updated_at = now
    return event


async def _news(session: AsyncSession, today: date) -> list[_News]:
    """List each user's confirmed, upcoming, not-already-known watched guests."""
    guest_cancelled = func.max(
        case((AppearanceSource.status == "cancelled", 1), else_=0)
    )
    rows = await session.execute(
        select(
            Match.user_id,
            Appearance.convention_id,
            Match.person_id,
            Convention.start_date,
            Convention.end_date,
            Convention.status,
            guest_cancelled,
        )
        .join(Appearance, Appearance.id == Match.appearance_id)
        .join(Convention, Convention.id == Appearance.convention_id)
        .join(Watch, Watch.id == Match.watch_id)
        .outerjoin(AppearanceSource, AppearanceSource.appearance_id == Appearance.id)
        .where(Match.kind == "confirmed", Convention.start_date >= today)
        .group_by(Match.user_id, Appearance.convention_id, Match.person_id)
        # Seen before the earliest watch that found them: already known, no alert.
        .having(func.min(Appearance.created_at) >= func.min(Watch.alerts_from))
        .order_by(Convention.start_date, Appearance.convention_id, Match.person_id)
    )
    return [
        _News(
            user_id=user_id,
            convention_id=convention_id,
            person_id=person_id,
            start_date=start,
            end_date=end,
            cancelled=status == "cancelled" or bool(guest),
        )
        for user_id, convention_id, person_id, start, end, status, guest in rows
    ]
