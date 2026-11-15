import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from casttrail.discovery.comiconomicon_refresh import JOB as COMICONOMICON_JOB
from casttrail.discovery.models import (
    Convention,
    ConventionSource,
    JobRun,
    SourceObservation,
)
from casttrail.discovery.refresh import JOB as ROSTERCON_JOB
from casttrail.providers.comiconomicon import PROVIDER_ID as COMICONOMICON_ID
from casttrail.providers.rostercon import PROVIDER_ID as ROSTERCON_ID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# A refresh that has not succeeded for this long is worth a warning.
STALE_AFTER = timedelta(days=3)
_RECENT = 10


@dataclass(frozen=True)
class Source:
    """A source the app refreshes from: its provider id, its run's name, its name."""

    provider: str
    job: str
    label: str


ROSTERCON = Source(ROSTERCON_ID, ROSTERCON_JOB, "Rostercon")
COMICONOMICON = Source(COMICONOMICON_ID, COMICONOMICON_JOB, "Comiconomicon")
#: In the order the Source health page shows them, which is the order they run.
SOURCES = (ROSTERCON, COMICONOMICON)


@dataclass(frozen=True)
class SourceHealth:
    """How one source is doing, worked out from what was recorded."""

    provider: str
    label: str
    conventions: int
    last_page_at: datetime | None
    last_run_at: datetime | None
    last_run_status: str | None
    last_success_at: datetime | None
    failures_in_a_row: int
    warnings: list[str] = field(default_factory=list)


def _counts(run: JobRun) -> dict[str, object]:
    try:
        value = json.loads(run.summary or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _number(counts: dict[str, object], key: str) -> int:
    value = counts.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _warnings(
    label: str, last: JobRun | None, success: JobRun | None, now: datetime
) -> list[str]:
    """Say, in plain words, what looks wrong with a source's latest runs."""
    if last is None:
        return ["No refresh has run yet."]
    counts = _counts(last)
    found: list[str] = []
    stopped = counts.get("stopped")
    if isinstance(stopped, str):
        found.append(f"The last refresh stopped early: {stopped}.")
    if _number(counts, "events_unreadable"):
        unread, kept = "Some event pages could not be read.", "Stored data was kept."
        found.append(f"{unread} {label} may have changed its layout. {kept}")
    if _number(counts, "events_failed") or _number(counts, "people_failed"):
        found.append("Some pages could not be fetched in the last refresh.")
    if _number(counts, "people_ambiguous") or _number(counts, "people_unmatched"):
        found.append(f"Some watched people have no single {label} page; skipped.")
    if success is None:
        found.append("No refresh has succeeded recently.")
    elif now - success.started_at > STALE_AFTER:
        found.append("The last successful refresh was more than 3 days ago.")
    return found


async def all_source_health(
    session: AsyncSession, *, now: datetime
) -> list[SourceHealth]:
    """Summarise every source, in the order they run.

    Args:
        session: The database session.
        now: The current time.

    Returns:
        One summary per source.
    """
    return [await source_health(session, source, now=now) for source in SOURCES]


async def source_health(
    session: AsyncSession, source: Source, *, now: datetime
) -> SourceHealth:
    """Summarise how one source's refresh is doing.

    A page that stopped parsing never removes stored data (it is counted and
    skipped), so a warning here means "look at this", not "data was lost".

    Args:
        session: The database session.
        source: The source.
        now: The current time.

    Returns:
        The summary, with plain-language warnings.
    """
    conventions = (
        await session.scalar(
            select(func.count(ConventionSource.id))
            .join(Convention, Convention.id == ConventionSource.convention_id)
            .where(ConventionSource.provider == source.provider)
        )
        or 0
    )
    last_page = await session.scalar(
        select(func.max(SourceObservation.fetched_at)).where(
            SourceObservation.provider == source.provider
        )
    )
    runs = list(
        await session.scalars(
            select(JobRun)
            .where(JobRun.job == source.job, JobRun.status != "running")
            .order_by(JobRun.started_at.desc())
            .limit(_RECENT)
        )
    )
    last = runs[0] if runs else None
    success = next((run for run in runs if run.status == "succeeded"), None)
    streak = 0
    for run in runs:
        if run.status != "failed":
            break
        streak += 1

    warnings = _warnings(source.label, last, success, now)
    return SourceHealth(
        provider=source.provider,
        label=source.label,
        conventions=conventions,
        last_page_at=last_page,
        last_run_at=last.started_at if last else None,
        last_run_status=last.status if last else None,
        last_success_at=success.started_at if success else None,
        failures_in_a_row=streak,
        warnings=warnings,
    )
