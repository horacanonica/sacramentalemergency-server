"""California local time.

Hardcoded to America/Los_Angeles (Pacific Time, PST/PDT). This parish
is in California. Day-off, vacation, recollection, the daily scheduler,
and history timestamps all use this zone. Do not use date.today() or
UTC calendar dates for those — a Tuesday day off would start Monday
evening California time if the container clock is UTC.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CALIFORNIA_TZ_NAME = "America/Los_Angeles"
CALIFORNIA_TZ = ZoneInfo(CALIFORNIA_TZ_NAME)
# Day off, recollection, and vacation all start at this hour the evening
# before the listed day and end at this hour on the day itself.
HANDOFF_HOUR = 20
RECOLLECTION_ENDS_HOUR = HANDOFF_HOUR  # backward-compatible alias


def california_now() -> datetime:
    return datetime.now(CALIFORNIA_TZ)


def california_today() -> date:
    return california_now().date()


def week_monday(day: date) -> date:
    """Monday of the California week containing day (Monday=0)."""
    return day - timedelta(days=day.weekday())


def effective_ring_date(at: date | datetime | None = None) -> date:
    """The calendar day the ring should treat as 'today'.

    After 8:00 PM California time, tomorrow's absences are already in
    effect (a Monday day off starts Sunday at 8pm and ends Monday at 8pm).
    """
    dt = as_california_datetime(at)
    if dt.hour >= HANDOFF_HOUR:
        return dt.date() + timedelta(days=1)
    return dt.date()


def as_california_datetime(value: date | datetime | None = None) -> datetime:
    """Normalize a date/datetime for availability checks.

    None → now in California. A naive datetime is treated as already
    California-local. A bare date is evaluated at noon that day so a
    recollection Wednesday still counts as "during recollection" (before
    8pm) when callers only have a calendar date.
    """
    if value is None:
        return california_now()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=CALIFORNIA_TZ)
        return value.astimezone(CALIFORNIA_TZ)
    return datetime(value.year, value.month, value.day, 12, 0, tzinfo=CALIFORNIA_TZ)


def format_california(value: str | datetime) -> str:
    """Render a stored timestamp in California time for the dashboard.

    Older history rows were written as UTC; newer ones are already
    California-offset. Both convert to the same local clock.
    """
    if isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CALIFORNIA_TZ).strftime("%Y-%m-%d %I:%M:%S %p %Z")
