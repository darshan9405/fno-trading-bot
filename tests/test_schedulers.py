"""Scheduler tests: lead generation, order placement, trade tracking (no network)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import select

from app import create_app
from app.broker.base import (
    BrokerBase,
    FillView,
    FundsView,
    HolidayView,
    InstrumentView,
    ProfileView,
)
from app.config import Config
from app.db import dispose, session_scope
from app.models import Instrument, Lead, MarketHoliday, Order, SpecialSession, Trade
from app.scheduler.lead_generator import run_lead_generator
from app.scheduler.order_placer import run_order_placer
from app.scheduler.trade_tracker import run_trade_tracker
from app.services import health_service, lead_service, trade_service
from app.services.killswitch_service import activate_killswitch
from app.strategy import LeadCandidate, Strategy, register_strategy

IST = ZoneInfo("Asia/Kolkata")


class FakeBroker(BrokerBase):
    def __init__(self):
        self.placed = []
        self.modified = []
        self.ltp_map = {}
        self.expiries = []
        self.contracts = []
        self.fills = {}
        self.holidays = []

    def get_historical_candles(self, instrument_key, interval, from_date, to_date):
        idx = pd.date_range(start="2026-01-01", periods=60, freq="D")
        return pd.DataFrame(
            {"open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0, "volume": 1000.0, "oi": 5000.0},
            index=idx,
        )

    def get_ltp(self, instrument_keys):
        return {k: self.ltp_map.get(k, 100.0) for k in instrument_keys}

    def place_order(self, order):
        self.placed.append(order)
        oid = f"o-{len(self.placed)}"
        avg = self.ltp_map.get(order.instrument_key, 100.0)
        self.fills[oid] = [
            FillView(trade_id=f"t-{len(self.placed)}", order_id=oid, quantity=order.quantity,
                     average_price=avg, transaction_type=order.transaction_type)
        ]
        return oid

    def modify_order(self, params):
        self.modified.append(params)

    def cancel_order(self, order_id):
        pass

    def exit_all(self, tag=None, segment=None):
        pass

    def get_positions(self):
        return []

    def get_funds(self):
        return FundsView(available_margin=100000.0)

    def get_order_book(self):
        return []

    def get_trades_by_order(self, order_id):
        return self.fills.get(order_id, [])

    def get_expiries(self, underlying_key):
        return self.expiries

    def get_option_contracts(self, underlying_key, expiry=None):
        return [c for c in self.contracts if c.underlying_key == underlying_key]

    def get_profile(self):
        return ProfileView(user_id="u1")

    def search_instruments(self, query):
        return []

    def get_market_holidays(self):
        return self.holidays

    def get_exchange_timings(self, day):
        return []


@register_strategy("test_breakout")
class TestBreakoutStrategy(Strategy):
    name = "test_breakout"
    required_interval = "day"

    def generate(self, instrument, candles, now):
        return [
            LeadCandidate(
                instrument_id=instrument.id,
                underlying_key=instrument.spot_instrument_key,
                direction="CALL",
                signal_type="horizontal_range",
                signal_level=100.0,
                confidence=0.9,
                chart_interval="day",
            )
        ]


def _now(hour, minute=0):
    return datetime(2026, 9, 4, hour, minute, tzinfo=IST)  # Friday


@pytest.fixture
def env(tmp_path):
    cfg = Config()
    cfg.RATE_LIMIT_ENABLED = False
    cfg.DATABASE_URL = f"sqlite:///{tmp_path / 'sched.db'}"
    cfg.SECRET_KEY = "test-secret"
    cfg.JWT_MASTER_SECRET = "test-master"
    dispose()
    create_app(cfg)  # creates tables + seeds settings

    from app.settings import set_setting

    set_setting("strategy", "test_breakout")
    set_setting("trading_start", "10:00")
    set_setting("sqoff_time", "14:00")
    set_setting("initial_sl_pct", 10.0)
    set_setting("trail_activate_pct", 5.0)
    set_setting("trail_gap_pct", 5.0)
    set_setting("max_lead_price_divergence_pct", 0.5)
    set_setting("min_days_to_expiry", 5)
    set_setting("qty_lots_per_trade", 1)

    from app.auth import UpstoxTokenStore

    UpstoxTokenStore._token = None
    UpstoxTokenStore._loaded = False

    with session_scope() as session:
        session.add_all(
            [
                Instrument(symbol="NIFTY", exchange="NSE", segment="NSE_INDEX",
                           spot_instrument_key="NSE_INDEX|Nifty 50", instrument_token="26000",
                           trading_symbol="NIFTY", lot_size=50, enabled=True),
                Instrument(symbol="RELIANCE", exchange="NSE", segment="NSE_EQ",
                           spot_instrument_key="NSE_EQ|INE002A01018", instrument_token="2885",
                           trading_symbol="RELIANCE", lot_size=1250, enabled=True),
            ]
        )
    yield
    dispose()


def _seed_broker(env):
    broker = FakeBroker()
    broker.expiries = [__import__("datetime").date(2026, 9, 10), __import__("datetime").date(2026, 9, 17)]
    broker.contracts = [
        InstrumentView(instrument_key="NSE_FO|84123", trading_symbol="NIFTY 10 SEP 26 26800 CE",
                       instrument_type="CE", expiry=__import__("datetime").date(2026, 9, 10),
                       strike_price=26800.0, lot_size=50, underlying_key="NSE_INDEX|Nifty 50"),
        InstrumentView(instrument_key="NSE_FO|90111", trading_symbol="RELIANCE 10 SEP 26 3000 PE",
                       instrument_type="PE", expiry=__import__("datetime").date(2026, 9, 10),
                       strike_price=3000.0, lot_size=1250, underlying_key="NSE_EQ|INE002A01018"),
    ]
    return broker


def _open_nifty_trade(env, broker, entry=100.0):
    run_lead_generator(broker=broker, now=_now(10, 30))
    run_order_placer(broker=broker, now=_now(10, 35))
    with session_scope() as session:
        return session.execute(select(Trade).where(Trade.status == "open")).scalars().first()


# --- Scheduler 1: lead generation ----------------------------------------


def test_lead_generator_writes_queued_leads_and_dedups(env):
    broker = _seed_broker(env)
    run_lead_generator(broker=broker, now=_now(10, 30))
    leads = lead_service.get_queued_leads()
    assert len(leads) == 2
    assert all(l.status == "queued" for l in leads)
    assert all(l.strategy == "test_breakout" for l in leads)

    run_lead_generator(broker=broker, now=_now(10, 45))  # dedup: same day
    assert len(lead_service.get_queued_leads()) == 2


def test_lead_generator_skips_outside_window(env):
    broker = _seed_broker(env)
    run_lead_generator(broker=broker, now=_now(9, 0))
    run_lead_generator(broker=broker, now=_now(14, 30))
    assert lead_service.get_queued_leads() == []


def test_lead_generator_touches_heartbeat(env):
    run_lead_generator(broker=_seed_broker(env), now=_now(10, 30))
    assert health_service.last_heartbeat("lead_generator") is not None


def test_lead_generator_skips_on_holiday(env):
    broker = _seed_broker(env)
    from app.settings import set_setting

    # mark today as already synced (should_sync uses real IST now) so the manual holiday survives
    set_setting("market_calendar_last_sync_date", health_service.now_ist().date().isoformat())
    with session_scope() as session:
        session.add(MarketHoliday(date=_now(10, 30).date(), note="NSE holiday"))
    run_lead_generator(broker=broker, now=_now(10, 30))
    assert lead_service.get_queued_leads() == []
    assert health_service.last_heartbeat("lead_generator").note == "outside trading window"


# --- Scheduler 3: order placement ----------------------------------------


def test_order_placer_opens_trade_with_sl(env):
    broker = _seed_broker(env)
    _open_nifty_trade(env, broker)

    with session_scope() as session:
        trades = session.execute(select(Trade)).scalars().all()
        assert len(trades) == 1
        trade = trades[0]
        assert trade.direction == "CALL"
        assert trade.option_instrument_key == "NSE_FO|84123"
        assert trade.entry_price == 100.0
        assert trade.initial_sl == 90.0
        assert trade.current_sl == 90.0
        assert trade.quantity == 50
        assert trade.entry_order_id and trade.sl_order_id
        assert trade.best_price == 100.0

        lead = session.get(Lead, trade.lead_id)
        assert lead.status == "placed"

        orders = session.execute(select(Order)).scalars().all()
        assert sorted(o.order_type for o in orders) == ["MARKET", "SL-M"]

    # RELIANCE lead skipped (no CE contract for a CALL)
    with session_scope() as session:
        skipped = session.execute(select(Lead).where(Lead.status == "skipped")).scalars().all()
        assert len(skipped) == 1
        assert "no CALL contract" in skipped[0].note


def test_order_placer_skips_on_price_divergence(env):
    broker = _seed_broker(env)
    broker.ltp_map["NSE_INDEX|Nifty 50"] = 102.0  # 2% > 0.5% threshold
    run_lead_generator(broker=broker, now=_now(10, 30))
    run_order_placer(broker=broker, now=_now(10, 35))
    with session_scope() as session:
        assert session.execute(select(Trade)).scalars().first() is None
        skipped = session.execute(select(Lead).where(Lead.status == "skipped")).scalars().all()
        assert len(skipped) == 2
        assert "diverged" in skipped[0].note


def test_order_placer_halts_on_killswitch(env):
    broker = _seed_broker(env)
    activate_killswitch(reason="test")
    run_lead_generator(broker=broker, now=_now(10, 30))
    run_order_placer(broker=broker, now=_now(10, 35))
    with session_scope() as session:
        assert session.execute(select(Trade)).scalars().first() is None
    assert health_service.last_heartbeat("order_placer").note == "killswitch active"


# --- Scheduler 2: trade tracking -----------------------------------------


def test_trade_tracker_trails_stop_loss(env):
    broker = _seed_broker(env)
    trade = _open_nifty_trade(env, broker)

    broker.ltp_map["NSE_FO|84123"] = 100.0  # no move
    run_trade_tracker(broker=broker, now=_now(11, 0))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        assert t.current_sl == 90.0
        assert t.trail_state == "at_initial"

    broker.ltp_map["NSE_FO|84123"] = 106.0  # +6% -> breakeven
    run_trade_tracker(broker=broker, now=_now(11, 1))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        assert t.current_sl == 100.0
        assert t.trail_state == "breakeven"

    broker.ltp_map["NSE_FO|84123"] = 110.0  # best=110 -> trail 5% = 104.5
    run_trade_tracker(broker=broker, now=_now(11, 2))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        assert t.current_sl == 104.5
        assert t.trail_state == "trailing"

    assert [m.trigger_price for m in broker.modified] == [100.0, 104.5]


def test_trade_tracker_closes_on_sl_hit(env):
    broker = _seed_broker(env)
    trade = _open_nifty_trade(env, broker)

    broker.ltp_map["NSE_FO|84123"] = 89.0  # below initial SL 90
    # model the broker executing the SL order at 89
    broker.fills[trade.sl_order_id] = [
        FillView(trade_id="t-sl", order_id=trade.sl_order_id, quantity=trade.quantity,
                 average_price=89.0, transaction_type="SELL")
    ]
    run_trade_tracker(broker=broker, now=_now(11, 0))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        assert t.status == "closed"
        assert t.exit_reason == "sl_hit"
        assert t.exit_price == 89.0
        assert t.realized_pnl == (89.0 - 100.0) * 50


def test_trade_tracker_squares_off_at_window_end(env):
    broker = _seed_broker(env)
    trade = _open_nifty_trade(env, broker)

    run_trade_tracker(broker=broker, now=_now(14, 30))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        assert t.status == "closed"
        assert t.exit_reason == "sqoff"


def test_trade_tracker_respects_special_session_end(env):
    broker = _seed_broker(env)
    with session_scope() as session:
        session.add(SpecialSession(date=_now(10, 30).date(), start="09:00", end="13:00", note="half-day"))
    trade = _open_nifty_trade(env, broker)

    run_trade_tracker(broker=broker, now=_now(12, 30))  # before special end 13:00
    with session_scope() as session:
        assert session.get(Trade, trade.id).status == "open"

    run_trade_tracker(broker=broker, now=_now(13, 30))  # after special end
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        assert t.status == "closed"
        assert t.exit_reason == "sqoff"


# --- trailing math unit ---------------------------------------------------


def test_compute_trailing_sl_unit(env):
    from app.services.trade_service import compute_trailing_sl

    trade = _open_nifty_trade(env, _seed_broker(env))  # entry 100, sl 90, best 100
    trade.best_price = 107.0
    new_sl, state = compute_trailing_sl(trade, 107.0, activate_pct=5.0, gap_pct=5.0)
    assert new_sl == 100.0 and state == "breakeven"

    trade.trail_state = "breakeven"
    trade.best_price = 115.0
    new_sl, state = compute_trailing_sl(trade, 115.0, activate_pct=5.0, gap_pct=5.0)
    assert new_sl == 109.25 and state == "trailing"