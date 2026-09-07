"""Market calendar tests: weekends, holidays, special sessions, sync."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import create_app
from app.broker.base import HolidayView
from app.config import Config
from app.db import dispose, session_scope
from app.models import MarketHoliday, SpecialSession
from app.services import market_calendar
from app.settings import set_setting

IST = ZoneInfo("Asia/Kolkata")


def _dt(hour, minute=0, day=4):
    return datetime(2026, 9, day, hour, minute, tzinfo=IST)


@pytest.fixture
def env(tmp_path):
    cfg = Config()
    cfg.RATE_LIMIT_ENABLED = False
    cfg.DATABASE_URL = f"sqlite:///{tmp_path / 'cal.db'}"
    cfg.SECRET_KEY = "test-secret"
    cfg.JWT_MASTER_SECRET = "test-master"
    dispose()
    create_app(cfg)  # tables + settings + seeded calendar defaults
    set_setting("trading_start", "10:00")
    set_setting("sqoff_time", "14:00")
    set_setting("market_calendar_last_sync_date", "")
    yield
    dispose()


def test_weekend_is_not_trading(env):
    assert market_calendar.trading_hours(_dt(10).date()) is not None  # Fri 2026-09-04
    assert market_calendar.is_market_open(_dt(10)) is True
    sat = datetime(2026, 9, 5, 10, 0, tzinfo=IST)
    sun = datetime(2026, 9, 6, 10, 0, tzinfo=IST)
    assert market_calendar.trading_hours(sat.date()) is None
    assert market_calendar.trading_hours(sun.date()) is None
    assert market_calendar.is_market_open(sat) is False
    assert market_calendar.is_market_open(sun) is False


def test_holiday_is_not_trading(env):
    day = _dt(10).date()
    with session_scope() as session:
        session.add(MarketHoliday(date=day, note="NSE holiday"))
    assert market_calendar.trading_hours(day) is None
    assert market_calendar.is_market_open(_dt(10)) is False


def test_special_session_overrides_window(env):
    day = _dt(10).date()
    with session_scope() as session:
        session.add(SpecialSession(date=day, start="09:00", end="13:00", note="half-day"))
    assert market_calendar.trading_hours(day) == (datetime(2026, 9, 4).replace(hour=9, minute=0).time(), datetime(2026, 9, 4).replace(hour=13, minute=0).time())
    assert market_calendar.is_market_open(_dt(12, 0)) is True
    assert market_calendar.is_market_open(_dt(13, 30)) is False
    assert market_calendar.session_end(day).strftime("%H:%M") == "13:00"


def test_default_window_from_settings(env):
    set_setting("sqoff_time", "15:30")
    assert market_calendar.session_end(_dt(10).date()).strftime("%H:%M") == "15:30"


def test_non_padded_hour_is_accepted(env):
    set_setting("trading_start", "9:30")
    set_setting("sqoff_time", "9:45")
    assert market_calendar.session_start(_dt(10).date()).strftime("%H:%M") == "09:30"
    assert market_calendar.session_end(_dt(10).date()).strftime("%H:%M") == "09:45"


def test_sync_from_broker_populates_holidays(env):
    class FakeCalendarBroker:
        def get_market_holidays(self):
            return [
                HolidayView(date=datetime(2026, 9, 4, tzinfo=IST).date(), description="Ganesh Chaturthi",
                            closed_exchanges=["NSE", "BSE"]),
                HolidayView(date=datetime(2026, 9, 17, tzinfo=IST).date(), description="Special morning session",
                            open_exchanges=[]),
            ]

    count = market_calendar.sync_from_broker(FakeCalendarBroker())
    assert count == 1
    assert market_calendar.trading_hours(datetime(2026, 9, 4, tzinfo=IST).date()) is None
    # NSE-open day is NOT a holiday -> uses default window
    assert market_calendar.trading_hours(datetime(2026, 9, 17, tzinfo=IST).date()) is not None
    assert market_calendar.should_sync() is False


def test_sync_clears_stale_year_holidays(env):
    jan1 = datetime(2026, 1, 1, tzinfo=IST).date()  # Thursday (weekday)
    with session_scope() as session:
        session.add(MarketHoliday(date=jan1, note="stale"))
    assert market_calendar.trading_hours(jan1) is None  # blocked by stale row

    class FakeCalendarBroker:
        def get_market_holidays(self):
            return [HolidayView(date=datetime(2026, 9, 4, tzinfo=IST).date(), description="H",
                                closed_exchanges=["NSE"])]

    market_calendar.sync_from_broker(FakeCalendarBroker())
    # stale Jan-1 holiday removed; only the synced Sep-4 holiday remains.
    assert market_calendar.trading_hours(jan1) is not None
    assert market_calendar.trading_hours(datetime(2026, 9, 4, tzinfo=IST).date()) is None