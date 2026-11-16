import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update

from casttrail.catalogue.models import Person
from casttrail.db import utc_now
from casttrail.discovery.models import Convention
from casttrail.notifications.models import NotificationEvent, UserNotification

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Enough for years of alerts at this scale; older ones drop off the page, not the table.
PAGE_SIZE = 200


@dataclass(frozen=True)
class Alert:
    """One of a user's alerts, as the Alerts page shows it."""

    id: uuid.UUID
    person_name: str
    convention: Convention
    raised_at: datetime
    unread: bool
    # Raised again because the dates changed or it was cancelled.
    changed: bool
    cancelled: bool


async def unread_count(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Count a user's unread alerts.

    Args:
        session: The database session.
        user_id: The signed-in user.

    Returns:
        How many of their alerts are unread.
    """
    count = await session.scalar(
        select(func.count(UserNotification.id)).where(
            UserNotification.user_id == user_id, UserNotification.read_at.is_(None)
        )
    )
    return count or 0


async def user_alerts(session: AsyncSession, user_id: uuid.UUID) -> list[Alert]:
    """List a user's alerts, the most recently raised first.

    Args:
        session: The database session.
        user_id: The signed-in user. Only their alerts are read.

    Returns:
        Up to ``PAGE_SIZE`` alerts.
    """
    rows = await session.execute(
        select(UserNotification, NotificationEvent, Person.name, Convention)
        .join(NotificationEvent, NotificationEvent.id == UserNotification.event_id)
        .join(Person, Person.id == NotificationEvent.person_id)
        .join(Convention, Convention.id == NotificationEvent.convention_id)
        .where(UserNotification.user_id == user_id)
        .order_by(UserNotification.raised_at.desc(), UserNotification.id)
        .limit(PAGE_SIZE)
    )
    return [
        Alert(
            id=alert.id,
            person_name=name,
            convention=convention,
            raised_at=alert.raised_at,
            unread=alert.read_at is None,
            changed=alert.rev > 1,
            cancelled=event.cancelled,
        )
        for alert, event, name, convention in rows
    ]


async def mark_read(
    session: AsyncSession, user_id: uuid.UUID, alert_id: uuid.UUID
) -> bool:
    """Mark one of a user's alerts read. Marking it again changes nothing.

    Args:
        session: The database session.
        user_id: The signed-in user. Another user's alert is never touched.
        alert_id: The alert.

    Returns:
        Whether the user has such an alert.
    """
    alert = await session.scalar(
        select(UserNotification).where(
            UserNotification.id == alert_id, UserNotification.user_id == user_id
        )
    )
    if alert is None:
        await session.rollback()
        return False
    if alert.read_at is None:
        alert.read_at = utc_now()
    await session.commit()
    return True


async def mark_all_read(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Mark every unread alert of a user read.

    Args:
        session: The database session.
        user_id: The signed-in user. Only their alerts are touched.

    Returns:
        How many were marked.
    """
    result = await session.execute(
        update(UserNotification)
        .where(UserNotification.user_id == user_id, UserNotification.read_at.is_(None))
        .values(read_at=utc_now())
    )
    await session.commit()
    return int(result.rowcount)  # type: ignore[attr-defined]
