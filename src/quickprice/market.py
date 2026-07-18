"""Small conservative market-session helpers for cache freshness decisions."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .domain import UTC, ensure_utc
from .plugin_api import MarketCalendar

_NEW_YORK = ZoneInfo("America/New_York")


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
