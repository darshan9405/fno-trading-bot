"""Indian market calendar: weekends, NSE holidays, and special session times.

Single source of truth for "is the market open right now?".
- weekends are never trading days
- `market_holidays` table holds non-trading days (seeded with fixed civil
  holidays; synced from Upstox `get_holidays()` once per day)
- `special_sessions` table overrides the daily window (special/half-day sessions)

The default trading window comes from the `trading_start` / `sqoff_time`
settings.
"""

import logging
from datetime import date, datetime, time

from sqlalchemy import select

from app.db import session_scope
from app.models import MarketHoliday, SpecialSession
from app.services.health_service import now_ist
from app.settings import get_setting, set_setting

log = logging.getLogger(__name__)

# Fixed civil holidays (calendar-fixed, computed for the current year). Used as
# a baseline before the Upstox sync; lunar/misc holidays come from the sync.
_FIXED_HOLIDAYS = {
    1: (26, "Republic Day"),
    5: (1, "Maharashtra Day"),
    8: (15, "Independence Day"),
    10: (2, "Gandhi Jayanti"),
    12: (25, "Christmas"),
}


def _parse_time(value: str) -> time:
    """Parse a setting time, tolerating non-zero-padded hours like '9:30'."""
    parts = str(value).split(":")
    if len(parts) == 2 and parts[0].isdigit() and len(parts[0]) == 1:
        value = f"0{value}"
    return time.fromisoformat(value)


def _default_window() -> tuple[time, time]:
    return _parse_time(get_setting("trading_start", "10:00")), _parse_time(get_setting("sqoff_time", "14:00"))


def trading_hours(day: date) -> tuple[time, time] | None:
    """(start, end) for a trading day, else None (weekend or holiday)."""
    if day.weekday() >= 5:
        return None
    with session_scope() as session:
        if session.get(MarketHoliday, day) is not None:
            return None
        special = session.get(SpecialSession, day)
        if special is not None:
            return _parse_time(special.start), _parse_time(special.end)
    return _default_window()


def is_market_open(now: datetime | None = None) -> bool:
    now = now or now_ist()
    hours = trading_hours(now.date())
    if hours is None:
        return False
    start, end = hours
    start_dt = datetime.combine(now.date(), start, tzinfo=now.tzinfo)
    end_dt = datetime.combine(now.date(), end, tzinfo=now.tzinfo)
    return start_dt <= now < end_dt


def session_start(day: date) -> time:
    hours = trading_hours(day)
    return hours[0] if hours else _default_window()[0]


def session_end(day: date) -> time:
    hours = trading_hours(day)
    return hours[1] if hours else _default_window()[1]


def seed_defaults() -> None:
    """Seed fixed civil holidays for the current year when the table is empty."""
    with session_scope() as session:
        if session.execute(select(MarketHoliday)).scalars().first() is not None:
            return
        today = now_ist().date()
        for month, (day, note) in _FIXED_HOLIDAYS.items():
            session.add(MarketHoliday(date=date(today.year, month, day), note=note))


def should_sync() -> bool:
    return get_setting("market_calendar_last_sync_date", "") != now_ist().date().isoformat()


def sync_from_broker(broker) -> int:
    """Pull NSE holidays from Upstox for the current year; returns rows synced.

    Days where NSE is listed in `closed_exchanges` become holidays. Days where
    NSE appears in `open_exchanges` (trading on a nominal holiday) are NOT
    holidays and simply use the configured default window (exact special times
    can be added manually via `special_sessions`).
    """
    holidays = broker.get_market_holidays()
    year = now_ist().date().year
    year_start, year_end = date(year, 1, 1), date(year, 12, 31)

    with session_scope() as session:
        for row in session.execute(
            select(MarketHoliday).where(MarketHoliday.date >= year_start, MarketHoliday.date <= year_end)
        ).scalars():
            session.delete(row)

        count = 0
        for h in holidays:
            if h.date is None or h.date.year != year:
                continue
            if "NSE" in (h.closed_exchanges or []):
                session.add(MarketHoliday(date=h.date, note=(h.description or "NSE holiday")[:128]))
                count += 1
            elif h.open_exchanges:
                log.info("special session day %s (NSE open): %s", h.date, h.description)

    set_setting("market_calendar_last_sync_date", now_ist().date().isoformat())
    log.info("market calendar synced: %d NSE holidays", count)
    return count