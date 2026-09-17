"""Time arithmetic against StudyLife's naive-local timestamps.

StudySessionDto.StartTime/EndTime are C# DateTime values that carry no offset in the JSON, and
SessionService treats them as the SERVER's local wall clock on purpose (see its Z1 comments and
docs/ARCHITECTURE.md "Single-Timezone Invariant"). studylife-web runs Europe/Berlin; this bot's
pod would otherwise run UTC, so parsing those strings with the process clock would put every
reminder two hours off for half the year and one hour off for the other half.

Everything here is therefore explicit about the zone rather than relying on TZ being set, and
pure, so the off-by-an-hour cases are testable without a server.
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def zone(name: str) -> tzinfo:
    """The configured zone, falling back to UTC if the name is unknown.

    A bad zone name must not take the bot down: commands would still work, only the reminder
    times would be wrong, and a crash-looping pod makes that harder to notice, not easier.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def parse_local(value: Any, tz: tzinfo) -> datetime | None:
    """One StudyLife timestamp as an aware datetime, or None if it is unusable.

    An offset IS honoured when present - the server does not send one today, but a future
    version that starts to would otherwise be misread by exactly the offset it just added.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed


def day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Midnight-to-midnight around `now`, in `now`'s own zone."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def sum_hours_on(sessions: list[dict[str, Any]], now: datetime, tz: tzinfo) -> float:
    """Hours studied on `now`'s calendar day.

    Summed here because the metrics API carries no daily figure at all - MetricsHoursDto has
    week, month, total and totalSessions only. Reading a "today" key off it returns None, which
    is how this used to render as a dash.

    A session that spans midnight is clipped to the day, so the two days it touches do not both
    count the whole of it.
    """
    start, end = day_bounds(now)
    total = 0.0
    for session in sessions or []:
        if not isinstance(session, dict):
            continue
        began = parse_local(session.get("startTime"), tz)
        ended = parse_local(session.get("endTime"), tz)
        if began is None or ended is None or ended <= began:
            continue
        overlap = (min(ended, end) - max(began, start)).total_seconds()
        if overlap > 0:
            total += overlap
    return total / 3600.0
