"""Small conservative market-session helpers for cache freshness decisions."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .domain import UTC, ensure_utc
from .plugin_api import MarketCalendar

_NEW_YORK = ZoneInfo("America/New_York")


def seconds_until_next_us_equity_open(when: datetime) -> float:
    """Return seconds until the next scheduled regular-session open.

    The project deliberately uses a weekday schedule rather than pretending to
    have a holiday calendar. A value loaded before 09:30 New York time expires
    at that day's open; a value loaded during or after a session expires at the
    next weekday's open.
    """

    current_utc = ensure_utc(when)
    local = current_utc.astimezone(_NEW_YORK)
    for days_ahead in range(8):
        day = (local + timedelta(days=days_ahead)).date()
        if day.weekday() >= 5:
            continue
        candidate = datetime.combine(day, time(9, 30), tzinfo=_NEW_YORK)
        if candidate > local:
            return max(1.0, (candidate.astimezone(UTC) - current_utc).total_seconds())
    raise RuntimeError("unable to resolve the next US equity session open")


def scheduled_market_status(calendar: MarketCalendar | str, when: datetime) -> str:
    """Return a conservative regular-session status without a holiday calendar."""

    calendar = MarketCalendar(calendar)
    local = ensure_utc(when).astimezone(_NEW_YORK)
    minute = local.hour * 60 + local.minute
    if calendar is MarketCalendar.US_EQUITY:
        return "open" if local.weekday() < 5 and 570 <= minute < 960 else "closed"
    if calendar is MarketCalendar.FX_24X5:
        weekday = local.weekday()
        is_open = (
            (weekday == 6 and minute >= 1020)
            or weekday in {0, 1, 2, 3}
            or (weekday == 4 and minute < 1020)
        )
        return "open" if is_open else "closed"
    return "open"


def most_recent_scheduled_close(calendar: MarketCalendar | str, when: datetime) -> datetime | None:
    """Find the close that begins the current/most recent closed interval."""

    calendar = MarketCalendar(calendar)
    if calendar is MarketCalendar.ALWAYS_OPEN:
        return None
    current = ensure_utc(when).astimezone(_NEW_YORK)
    close_at = time(16, 0) if calendar is MarketCalendar.US_EQUITY else time(17, 0)
    close_weekdays = set(range(5)) if calendar is MarketCalendar.US_EQUITY else {4}
    for days_back in range(8):
        day = (current - timedelta(days=days_back)).date()
        if day.weekday() not in close_weekdays:
            continue
        candidate = datetime.combine(day, close_at, tzinfo=_NEW_YORK)
        if candidate <= current:
            return candidate.astimezone(UTC)
    return None
