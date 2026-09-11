"""Scheduler tests: lead generation, order placement, trade tracking (no network)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import select

from app import create_app
from app.broker.base import (
    BrokerBase,
    BrokerError,
    FillView,
    FundsView,
    HolidayView,
    InstrumentView,
    OrderView,
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
        # Order book mirrors Upstox OrderView: order_id, status, average_price, quantity.
        self._order_book: list = []
        # Tests can opt a specific order id (or '*' for all) into a non-fill status
        # (e.g. "open" / "rejected") to exercise the LIMIT-poll path.
        self.fill_status_override: dict[str, str] = {}

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
        # Mirrors broker behaviour: LIMIT/MARKET fill immediately, SL orders sit
        # open until their trigger fires. Tests can override per-order via
        # fill_status_override (e.g. to simulate a filled SL or a rejected order).
        if order.order_type in ("SL", "SL-M"):
            default_status = "open"
        else:
            default_status = "complete"
        status = self.fill_status_override.get(oid) or self.fill_status_override.get("*") or default_status
        self._order_book.append(OrderView(
            order_id=oid, status=status, average_price=avg if status == "complete" else None,
            quantity=order.quantity, filled_quantity=order.quantity if status == "complete" else 0,
        ))
        self.fills[oid] = [
            FillView(trade_id=f"t-{len(self.placed)}", order_id=oid, quantity=order.quantity,
                     average_price=avg, transaction_type=order.transaction_type)
        ]
        return oid

    def modify_order(self, params):
        self.modified.append(params)
        # Real broker keeps an open SL in "open" status after a modify; the
        # trailing rule just changes trigger/price, not lifecycle.
        for o in self._order_book:
            if o.order_id == params.order_id and o.status == "open":
                break

    def cancel_order(self, order_id):
        pass

    def exit_all(self, tag=None, segment=None):
        pass

    def get_positions(self):
        return []

    def get_funds(self):
        return FundsView(available_margin=100000.0)

    def get_order_book(self):
        return list(self._order_book)

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
    set_setting("trail_activate_pct", 20.0)
    set_setting("trail_gap_pct", 10.0)
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
    # Spot LTP for divergence check; contract LTP for LIMIT pricing.
    broker.ltp_map = {
        "NSE_INDEX|Nifty 50": 100.0,
        "NSE_FO|84123": 100.0,
        "NSE_FO|90111": 100.0,
    }
    return broker


def _open_nifty_trade(env, broker, entry=100.0):
    run_lead_generator(broker=broker, now=_now(10, 30))
    run_order_placer(broker=broker, now=_now(10, 35))
    with session_scope() as session:
        return session.execute(select(Trade).where(Trade.status == "open")).scalars().first()


# --- Scheduler 1: lead generation ----------------------------------------


def test_lead_generator_writes_queued_leads_and_does_not_dedup(env):
    broker = _seed_broker(env)
    run_lead_generator(broker=broker, now=_now(10, 30))
    leads = lead_service.get_queued_leads()
    assert len(leads) == 2
    assert all(l.status == "queued" for l in leads)
    assert all(l.strategy == "test_breakout" for l in leads)

    # A second pass on the same day generates again — dedup happens at order placement.
    run_lead_generator(broker=broker, now=_now(10, 45))
    assert len(lead_service.get_queued_leads()) == 4


def test_lead_generator_attaches_fno_plan(env):
    from datetime import date

    broker = _seed_broker(env)
    run_lead_generator(broker=broker, now=_now(10, 30))

    with session_scope() as session:
        nifty = session.execute(
            select(Lead).where(Lead.underlying_key == "NSE_INDEX|Nifty 50")
        ).scalars().first()
        reliance = session.execute(
            select(Lead).where(Lead.underlying_key == "NSE_EQ|INE002A01018")
        ).scalars().first()

        # Deterministic check: resolve plans with a fixed reference date.
        from app.services import lead_service

        leads = [nifty, reliance]
        lead_service.attach_lead_plans(session, broker, leads, min_days=5, lots=2, today=date(2026, 9, 4))

        assert nifty.plan == {
            "expiry": "2026-09-10",
            "strike_price": 26800.0,
            "option_type": "CE",
            "trading_symbol": "NIFTY 10 SEP 26 26800 CE",
            "lot_size": 50,
            "quantity": 100,
            "spot": 100.0,
            "premium": 100.0,
            "margin_needed": 10000.0,
        }
        assert reliance.plan is None  # only a PE contract exists for RELIANCE


def test_lead_generator_persists_plan_without_blocking(env):
    """Plan resolution failures (e.g. no broker data) must not drop leads."""
    broker = _seed_broker(env)
    broker.contracts = []  # no option contracts -> plans stay None
    broker.expiries = []
    run_lead_generator(broker=broker, now=_now(10, 30))

    leads = lead_service.get_queued_leads()
    assert len(leads) == 2
    assert all(l.plan is None for l in leads)


def test_lead_generator_skips_lead_when_margin_insufficient(env):
    broker = _seed_broker(env)
    broker.get_funds = lambda: FundsView(available_margin=1000.0)  # can't afford a 5000 lot
    run_lead_generator(broker=broker, now=_now(10, 30))

    with session_scope() as session:
        nifty = session.execute(
            select(Lead).where(Lead.underlying_key == "NSE_INDEX|Nifty 50")
        ).scalars().first()
        reliance = session.execute(
            select(Lead).where(Lead.underlying_key == "NSE_EQ|INE002A01018")
        ).scalars().first()

        assert nifty.status == "skipped"
        assert "insufficient margin" in (nifty.note or "")
        assert nifty.plan is None  # nothing affordable -> no contract to plan
        assert reliance.status == "queued"  # no CE contract -> plan not resolvable, untouched


def test_lead_generator_keeps_lead_with_enough_margin(env):
    broker = _seed_broker(env)
    broker.get_funds = lambda: FundsView(available_margin=1_000_000.0)
    run_lead_generator(broker=broker, now=_now(10, 30))

    with session_scope() as session:
        nifty = session.execute(
            select(Lead).where(Lead.underlying_key == "NSE_INDEX|Nifty 50")
        ).scalars().first()
        assert nifty.status == "queued"
        assert "insufficient margin" not in (nifty.note or "")


def _option_chain(itype: str, strikes: list[float]):
    from datetime import date

    prefix = "c" if itype == "CE" else "p"
    return [
        InstrumentView(instrument_key=f"NSE_FO|{prefix}{i}", trading_symbol=f"NIFTY {itype} {s}",
                       instrument_type=itype, expiry=date(2026, 9, 10), strike_price=s,
                       lot_size=50, underlying_key="NSE_INDEX|Nifty 50")
        for i, s in enumerate(strikes)
    ]


def test_walk_candidates_put_goes_down_from_atm():
    from app.services.contract_service import walk_candidates

    pe = _option_chain("PE", [107.5, 110.0, 112.5, 115.0, 117.5, 120.0])
    got = [c.strike_price for c in walk_candidates(pe, "PUT", 117.5, 3)]
    assert got == [117.5, 115.0, 112.5, 110.0]


def test_walk_candidates_call_goes_up_from_atm():
    from app.services.contract_service import walk_candidates

    ce = _option_chain("CE", [110.0, 112.5, 115.0, 117.5, 120.0, 122.5, 125.0])
    got = [c.strike_price for c in walk_candidates(ce, "CALL", 117.5, 3)]
    assert got == [117.5, 120.0, 122.5, 125.0]


def test_select_affordable_prefers_atm_then_walks_otm():
    from app.services.contract_service import select_affordable, walk_candidates

    ce = _option_chain("CE", [110.0, 112.5, 115.0, 117.5, 120.0, 122.5, 125.0])
    candidates = list(walk_candidates(ce, "CALL", 117.5, 3))
    # index 3=117.5(ATM), 4=120, 5=122.5, 6=125 — OTM calls get cheaper upward
    premiums = {c.instrument_key: p for c, p in zip(ce, [30.0, 50.0, 80.0, 150.0, 115.0, 90.0, 60.0])}

    chosen, _, evaluated = select_affordable(candidates, premiums, available_margin=1_000_000, lots=1)
    assert chosen.strike_price == 117.5  # ATM wins when affordable
    assert evaluated is True

    chosen, _, evaluated = select_affordable(candidates, premiums, available_margin=6000, lots=1)
    assert chosen.strike_price == 120.0  # ATM too pricey (150*50=7500) -> walked up one

    chosen, cheapest, evaluated = select_affordable(candidates, premiums, available_margin=2000, lots=1)
    assert chosen is None  # cheapest 60*50=3000 still doesn't fit
    assert cheapest == 3000.0
    assert evaluated is True


def test_select_affordable_put_walks_down_not_up():
    from app.services.contract_service import select_affordable, walk_candidates

    pe = _option_chain("PE", [107.5, 110.0, 112.5, 115.0, 117.5, 120.0])
    candidates = list(walk_candidates(pe, "PUT", 117.5, 3))
    # index 4=117.5(ATM), 3=115, 2=112.5, 1=110 — OTM puts get cheaper downward
    premiums = {c.instrument_key: p for c, p in zip(pe, [50.0, 80.0, 110.0, 120.0, 220.0, 300.0])}

    chosen, _, _ = select_affordable(candidates, premiums, available_margin=6000, lots=1)
    assert chosen.strike_price == 115.0  # 220*50=11000 > 6000; walked DOWN to 115 (120*50=6000)


def test_margin_walk_put_117_5_goes_down(env):
    from datetime import date

    broker = _seed_broker(env)
    broker.contracts = _option_chain("PE", [107.5, 110.0, 112.5, 115.0, 117.5, 120.0])
    broker.ltp_map = {
        "NSE_INDEX|Nifty 50": 117.5,
        "NSE_FO|p4": 150.0,  # ATM 117.5
        "NSE_FO|p3": 120.0,  # 115
        "NSE_FO|p2": 90.0,   # 112.5
        "NSE_FO|p1": 60.0,   # 110
    }

    with session_scope() as session:
        inst = session.execute(select(Instrument).where(Instrument.symbol == "NIFTY")).scalars().first()
        lead = Lead(instrument_id=inst.id, underlying_key="NSE_INDEX|Nifty 50", direction="PUT",
                    strategy="breakout", signal_type="test", signal_level=117.5,
                    confidence=0.9, chart_interval="day", status="queued")
        session.add(lead)
        session.flush()
        lead_service.attach_lead_plans(
            session, broker, [lead], min_days=5, lots=1,
            today=date(2026, 9, 4), available_margin=6000.0,
        )

    with session_scope() as session:
        lead = session.execute(select(Lead)).scalars().first()
        assert lead.status == "queued"
        assert lead.plan["strike_price"] == 115.0  # ATM too pricey; walked DOWN, never up to 125
        assert lead.plan["spot"] == 117.5


def test_margin_walk_skips_when_nothing_affordable(env):
    from datetime import date

    broker = _seed_broker(env)
    broker.contracts = _option_chain("PE", [107.5, 110.0, 112.5, 115.0, 117.5, 120.0])
    broker.ltp_map = {
        "NSE_INDEX|Nifty 50": 117.5,
        "NSE_FO|p4": 150.0,
        "NSE_FO|p3": 120.0,
        "NSE_FO|p2": 90.0,
        "NSE_FO|p1": 60.0,
    }

    with session_scope() as session:
        inst = session.execute(select(Instrument).where(Instrument.symbol == "NIFTY")).scalars().first()
        lead = Lead(instrument_id=inst.id, underlying_key="NSE_INDEX|Nifty 50", direction="PUT",
                    strategy="breakout", signal_type="test", signal_level=117.5,
                    confidence=0.9, chart_interval="day", status="queued")
        session.add(lead)
        session.flush()
        lead_service.attach_lead_plans(
            session, broker, [lead], min_days=5, lots=1,
            today=date(2026, 9, 4), available_margin=2000.0,
        )

    with session_scope() as session:
        lead = session.execute(select(Lead)).scalars().first()
        assert lead.status == "skipped"
        assert "insufficient margin" in (lead.note or "")
        assert lead.plan is None


def test_lead_generator_skips_outside_window(env):
    broker = _seed_broker(env)
    run_lead_generator(broker=broker, now=_now(9, 0))
    run_lead_generator(broker=broker, now=_now(14, 30))
    assert lead_service.get_queued_leads() == []


def test_lead_generator_force_runs_outside_window(env):
    broker = _seed_broker(env)
    result = run_lead_generator(broker=broker, now=_now(9, 0), force=True)
    assert result == {"created": 2, "checked": 2}
    leads = lead_service.get_queued_leads()
    assert len(leads) == 2
    assert all(l.status == "queued" for l in leads)


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
        assert sorted(o.order_type for o in orders) == ["LIMIT", "SL-M"]

    assert [o.product for o in broker.placed] == ["D", "D"]  # delivery (NRML): LIMIT + SL-M
    assert [o.order_type for o in broker.placed] == ["LIMIT", "SL-M"]
    # LIMIT entry priced at LTP + 1% (default premium): 100.0 * 1.01 = 101.0.
    assert broker.placed[0].price == 101.0
    # SL-M: stop-loss-market; no limit price, only a trigger.
    assert broker.placed[1].order_type == "SL-M"
    assert broker.placed[1].price == 0.0
    assert broker.placed[1].trigger_price == 90.0

    # RELIANCE lead skipped (no CE contract for a CALL)
    with session_scope() as session:
        skipped = session.execute(select(Lead).where(Lead.status == "skipped")).scalars().all()
        assert len(skipped) == 1
        assert "no affordable contract" in skipped[0].note


def test_order_placer_places_no_sl_when_entry_fails(env):
    broker = _seed_broker(env)

    def fail_entry(order):
        raise BrokerError("entry rejected")

    broker.place_order = fail_entry
    run_lead_generator(broker=broker, now=_now(10, 30))
    run_order_placer(broker=broker, now=_now(10, 35))

    with session_scope() as session:
        assert session.execute(select(Trade)).scalars().first() is None
        skipped = session.execute(select(Lead).where(Lead.status == "skipped")).scalars().all()
        assert len(skipped) == 2
        assert "entry rejected" in skipped[0].note
    assert broker.placed == []  # no SELL / SL order was placed after the failed BUY


def test_order_placer_squares_off_when_sl_placement_fails(env):
    """If SL placement fails after entry fills, bot must not leave an unprotected
    long position. It places a LIMIT SELL (market orders are restricted on options)
    instead of cancelling (which Upstox rejects on a filled order)."""
    broker = _seed_broker(env)
    real_place = broker.place_order
    calls = []

    def selective_place(order):
        calls.append(order)
        # Reject SL-M with a message that matches the SL-M-rejection heuristic so
        # `place_stop_loss` falls back to SL; then reject SL with a generic msg.
        if order.order_type == "SL-M":
            raise BrokerError("SL-M (stop-loss-market) not supported for this segment")
        if order.order_type == "SL":
            raise BrokerError("SL rejected by broker")
        return real_place(order)

    broker.place_order = selective_place
    run_lead_generator(broker=broker, now=_now(10, 30))
    run_order_placer(broker=broker, now=_now(10, 35))

    with session_scope() as session:
        assert session.execute(select(Trade)).scalars().first() is None
        skipped = session.execute(select(Lead).where(Lead.status == "skipped")).scalars().all()
        assert any("sqoff=placed" in (s.note or "") for s in skipped)

    # Order sequence: LIMIT (entry), SL-M (rejected), SL (rejected), LIMIT SELL (sqoff).
    assert [o.order_type for o in calls] == ["LIMIT", "SL-M", "SL", "LIMIT"]
    assert calls[3].transaction_type == "SELL"
    assert calls[3].product == "D"
    # LTP=100, premium=1% -> sqoff price = 99.0 (slightly below LTP for fast fill).
    assert calls[3].price == 99.0


def test_order_placer_unfilled_entry_does_not_block_retry(env, monkeypatch):
    """An entry that is placed but never fills must not leave a phantom trade
    that blocks the underlying ("already traded today") for the rest of the day."""
    from app.scheduler import order_placer as op
    real_wait = op._wait_for_fill
    call_count = {"n": 0}

    def fake_wait(broker, order_id, timeout):
        call_count["n"] += 1
        # First call (LIMIT that doesn't fill): bail immediately.
        if call_count["n"] == 1:
            return None, "open"
        # Subsequent calls (the retry after regeneration): use the real poll.
        return real_wait(broker, order_id, timeout)

    monkeypatch.setattr(op, "_wait_for_fill", fake_wait)

    broker = _seed_broker(env)
    broker.get_trades_by_order = lambda order_id: []  # entry never fills
    broker.get_order_book = lambda: []
    run_lead_generator(broker=broker, now=_now(10, 30))
    run_order_placer(broker=broker, now=_now(10, 35))

    with session_scope() as session:
        assert session.execute(select(Trade)).scalars().first() is None
        skipped = session.execute(select(Lead).where(Lead.status == "skipped")).scalars().all()
        assert any("did not fill" in (s.note or "") for s in skipped)

    # Same day, leads regenerate and the broker fills -> the underlying trades.
    broker2 = _seed_broker(env)
    run_lead_generator(broker=broker2, now=_now(10, 40))
    run_order_placer(broker=broker2, now=_now(10, 45))
    with session_scope() as session:
        trades = session.execute(select(Trade)).scalars().all()
        assert len(trades) == 1
        assert trades[0].entry_order_id and trades[0].sl_order_id


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


def test_order_placer_skips_when_underlying_already_traded_today(env):
    broker = _seed_broker(env)
    with session_scope() as session:
        inst = session.execute(select(Instrument).where(Instrument.symbol == "NIFTY")).scalars().first()
        lead = Lead(instrument_id=inst.id, underlying_key="NSE_INDEX|Nifty 50", direction="CALL",
                    strategy="breakout", signal_type="test", signal_level=26800.0,
                    confidence=0.9, chart_interval="day", status="queued")
        session.add(lead)
        session.flush()
        session.add(Trade(lead_id=None, underlying_key="NSE_INDEX|Nifty 50",
                          option_instrument_key="NSE_FO|84123", option_instrument_token="84123",
                          tradingsymbol="NIFTY 10 SEP 26 26800 CE", lot_size=50, product="D",
                          direction="CALL", entry_price=100.0, quantity=50,
                          initial_sl=90.0, current_sl=90.0, trail_state="at_initial",
                          status="closed", entry_order_id="o-x", sl_order_id="o-y"))

    run_order_placer(broker=broker, now=_now(10, 35))
    with session_scope() as session:
        skipped = session.execute(select(Lead).where(Lead.status == "skipped")).scalars().first()
        assert skipped is not None
        assert "already traded today" in (skipped.note or "")
        assert session.execute(select(Trade).where(Trade.status == "open")).scalars().first() is None


def test_lead_generator_regenerates_after_skip(env):
    """A margin-skipped lead re-qualifies later in the day once funds allow."""
    broker = _seed_broker(env)
    broker.get_funds = lambda: FundsView(available_margin=1000.0)
    run_lead_generator(broker=broker, now=_now(10, 30))
    with session_scope() as session:
        nifty = session.execute(
            select(Lead).where(Lead.underlying_key == "NSE_INDEX|Nifty 50")
        ).scalars().first()
        assert nifty.status == "skipped"

    broker.get_funds = lambda: FundsView(available_margin=1_000_000.0)
    run_lead_generator(broker=broker, now=_now(11, 0))
    with session_scope() as session:
        nifty = session.execute(
            select(Lead).where(Lead.underlying_key == "NSE_INDEX|Nifty 50", Lead.status == "queued")
        ).scalars().first()
        assert nifty is not None
        assert nifty.plan is not None


# --- Scheduler 2: trade tracking -----------------------------------------


def test_trade_tracker_trails_stop_loss(env):
    """Trailing rule (v2):
    - SL stays at initial_sl (90) until ltp crosses 90 * 1.20 = 108 (activation).
    - After activation, SL = ltp * 0.9 and ratchets up on retracements.
    """
    broker = _seed_broker(env)
    trade = _open_nifty_trade(env, broker)

    broker.ltp_map["NSE_FO|84123"] = 100.0  # below activation (108) → SL unchanged
    run_trade_tracker(broker=broker, now=_now(11, 0))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        assert t.current_sl == 90.0
        assert t.trail_state == "at_initial"

    broker.ltp_map["NSE_FO|84123"] = 106.0  # still below activation
    run_trade_tracker(broker=broker, now=_now(11, 1))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        assert t.current_sl == 90.0
        assert t.trail_state == "at_initial"
        assert broker.modified == []  # no modify_order call yet

    broker.ltp_map["NSE_FO|84123"] = 108.0  # activation threshold = 90 * 1.20
    run_trade_tracker(broker=broker, now=_now(11, 2))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        # SL = 108 * 0.9 = 97.20, snapped to 0.05 tick band.
        assert t.current_sl == 97.20
        assert t.trail_state == "trailing"

    broker.ltp_map["NSE_FO|84123"] = 110.0  # SL = 110 * 0.9 = 99.0
    run_trade_tracker(broker=broker, now=_now(11, 3))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        assert t.current_sl == 99.0
        assert t.trail_state == "trailing"

    # Two modifies: the activation snap to 97.20, then the ratchet to 99.0.
    assert [m.trigger_price for m in broker.modified] == [97.20, 99.0]
    # Trailing is done by modifying the existing SL-M order (no limit price).
    assert all(m.order_type == "SL-M" for m in broker.modified)
    assert all(m.price == 0.0 for m in broker.modified)


def test_trade_tracker_ratchets_sl_on_retrace(env):
    """Once activated, the SL must NEVER move down on a retrace — gains are
    locked in. CALL entry=100, initial_sl=90, activation at ltp>=108.
    Sequence: 100 → 110 (SL=99) → 104 (candidate would be 93.6, ratchet holds 99).
    """
    broker = _seed_broker(env)
    trade = _open_nifty_trade(env, broker)

    broker.ltp_map["NSE_FO|84123"] = 110.0  # activate + first trail
    run_trade_tracker(broker=broker, now=_now(11, 0))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        assert t.current_sl == 99.0
        assert t.trail_state == "trailing"

    broker.ltp_map["NSE_FO|84123"] = 104.0  # retrace below prior SL — ratchet holds
    run_trade_tracker(broker=broker, now=_now(11, 1))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        assert t.current_sl == 99.0, "SL must not move down on retrace"
        assert t.trail_state == "trailing"

    # Only the activation snap modify happened — no second modify on the retrace.
    assert [m.trigger_price for m in broker.modified] == [99.0]


def test_trade_tracker_put_ratchets_down(env):
    """Symmetric PUT: initial_sl is above entry. activation_ltp = initial_sl * 0.8.
    After activation, SL moves DOWN only (in profitable direction)."""
    broker = _seed_broker(env)
    broker.ltp_map["NSE_FO|90111"] = 200.0  # RELIANCE PE contract for PUT leg
    broker.ltp_map["NSE_EQ|INE002A01018"] = 3000.0  # spot for divergence check
    # RELIANCE lot=1250 × premium=200 > FakeBroker default 100k; raise margin.
    broker.get_funds = lambda: FundsView(available_margin=1_000_000.0)
    # Seed a PUT lead manually (default test seeds a CALL for NIFTY).
    from datetime import date
    from app.models import Lead
    with session_scope() as session:
        reliance = session.execute(
            select(Instrument).where(Instrument.symbol == "RELIANCE")
        ).scalars().first()
        lead = Lead(
            instrument_id=reliance.id, underlying_key="NSE_EQ|INE002A01018",
            direction="PUT", strategy="test_breakout", signal_type="horizontal_range",
            signal_level=3000.0, confidence=0.9, chart_interval="day", status="queued",
        )
        session.add(lead)

    from app.scheduler.lead_generator import run_lead_generator as _rlg
    _rlg(broker=broker, now=_now(10, 30))
    from app.scheduler.order_placer import run_order_placer as _rop
    _rop(broker=broker, now=_now(10, 35))

    with session_scope() as session:
        trade = session.execute(select(Trade).where(Trade.status == "open")).scalars().first()
        assert trade.direction == "PUT"
        # entry 200, sl_pct 10% PUT → initial_sl = 200 * 1.10 = 220
        assert trade.entry_price == 200.0
        assert trade.initial_sl == 220.0

    # Below activation threshold: activation_ltp = 220 * (1 - 0.20) = 176.
    broker.ltp_map["NSE_FO|90111"] = 200.0  # not activated
    run_trade_tracker(broker=broker, now=_now(11, 0))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        assert t.current_sl == 220.0
        assert t.trail_state == "at_initial"

    # Activate + first trail at ltp=176 → candidate = 176 * 1.10 = 193.6
    broker.ltp_map["NSE_FO|90111"] = 176.0
    run_trade_tracker(broker=broker, now=_now(11, 1))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        assert t.current_sl == 193.6
        assert t.trail_state == "trailing"

    # Lower ltp → SL moves down: ltp=160 → candidate = 160 * 1.10 = 176.0
    broker.ltp_map["NSE_FO|90111"] = 160.0
    run_trade_tracker(broker=broker, now=_now(11, 2))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        assert t.current_sl == 176.0

    # Retrace UP → SL must not move up (ratchet): ltp=190 → candidate = 190*1.10 = 209.0
    # max(209, current 176) keeps 176 in CALL logic; symmetric for PUT we keep MIN.
    broker.ltp_map["NSE_FO|90111"] = 190.0
    run_trade_tracker(broker=broker, now=_now(11, 3))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        assert t.current_sl == 176.0, "PUT SL must not move up on retrace"


def test_trade_tracker_closes_on_sl_hit(env):
    broker = _seed_broker(env)
    trade = _open_nifty_trade(env, broker)

    broker.ltp_map["NSE_FO|84123"] = 89.0  # below initial SL 90
    # model the broker executing the SL order at 89
    broker.fills[trade.sl_order_id] = [
        FillView(trade_id="t-sl", order_id=trade.sl_order_id, quantity=trade.quantity,
                 average_price=89.0, transaction_type="SELL")
    ]
    for o in broker._order_book:
        if o.order_id == trade.sl_order_id:
            o.status = "complete"
            o.average_price = 89.0
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


def test_trade_tracker_replaces_cancelled_sl(env):
    """If the broker reports the SL as cancelled (e.g. user intervention or a
    broker-side cancel), trade_tracker must re-place a fresh SL instead of
    trailing a non-existent order."""
    broker = _seed_broker(env)
    trade = _open_nifty_trade(env, broker)

    old_sl = trade.sl_order_id
    # Simulate broker-side cancel: flip the SL order's status in the fake order book.
    for o in broker._order_book:
        if o.order_id == old_sl:
            o.status = "cancelled"
    placed_before = len(broker.placed)

    run_trade_tracker(broker=broker, now=_now(11, 0))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        assert t.status == "open"
        assert t.sl_order_id is not None and t.sl_order_id != old_sl
        assert t.current_sl == t.initial_sl

    # Exactly one new SL was placed (the replacement).
    assert len(broker.placed) == placed_before + 1
    assert broker.placed[-1].order_type == "SL-M"
    assert broker.placed[-1].transaction_type == "SELL"


def test_trade_tracker_closes_when_sl_status_complete(env):
    """When the broker reports the SL order as complete (it filled), trade_tracker
    must close the trade using the SL fill price — without placing another SL."""
    broker = _seed_broker(env)
    trade = _open_nifty_trade(env, broker)

    placed_before = len(broker.placed)
    # Mark SL complete at the broker + add a fill so exit price can be derived.
    for o in broker._order_book:
        if o.order_id == trade.sl_order_id:
            o.status = "complete"
            o.average_price = 89.0
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

    # No new SL placed — the existing one already filled at the broker.
    assert len(broker.placed) == placed_before


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


def test_trade_tracker_squares_off_when_no_sl_and_replacement_fails(env):
    """If a trade has no SL order and the bot can't place one (broker rejection,
    network error, etc.), the tracker sqoffs the trade with a LIMIT SELL — same
    defensive pattern as order_placer."""
    broker = _seed_broker(env)
    trade = _open_nifty_trade(env, broker)

    with session_scope() as session:
        t = session.get(Trade, trade.id)
        t.sl_order_id = None  # simulate SL was never placed / lost
    real_place = broker.place_order
    calls = []

    def selective_place(order):
        calls.append(order)
        if order.order_type == "SL-M":
            raise BrokerError("SL-M not supported for this segment")
        if order.order_type == "SL":
            raise BrokerError("SL rejected")
        return real_place(order)

    broker.place_order = selective_place
    placed_before = len(calls)

    run_trade_tracker(broker=broker, now=_now(11, 0))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        assert t.status == "closed"
        assert t.exit_reason == "no_sl_sqoff"

    # SL-M attempted (failed), then SL fallback (failed), then LIMIT SELL sqoff placed.
    assert [o.order_type for o in calls[placed_before:]] == ["SL-M", "SL", "LIMIT"]
    assert calls[-1].transaction_type == "SELL"
    # Sqoff priced at LTP - premium%: 100 * (1 - 0.01) = 99.0.
    assert calls[-1].price == 99.0


def test_trade_tracker_logs_critical_when_no_sl_and_sqoff_also_fails(env):
    """If the tracker can't place an SL AND the defensive sqoff also fails,
    the trade stays open and a CRITICAL log fires for manual intervention."""
    broker = _seed_broker(env)
    trade = _open_nifty_trade(env, broker)

    with session_scope() as session:
        t = session.get(Trade, trade.id)
        t.sl_order_id = None

    def fail_all(order):
        raise BrokerError("everything rejected")

    broker.place_order = fail_all
    run_trade_tracker(broker=broker, now=_now(11, 0))
    with session_scope() as session:
        t = session.get(Trade, trade.id)
        # Trade stays open so manual intervention can find and close it.
        assert t.status == "open"
        assert t.sl_order_id is None


# --- trailing math unit ---------------------------------------------------


def test_compute_trailing_sl_unit(env):
    from app.services.trade_service import compute_trailing_sl

    trade = _open_nifty_trade(env, _seed_broker(env))  # entry 100, initial_sl 90
    # Below activation: ltp=107, threshold = 90 * 1.20 = 108 → SL unchanged.
    new_sl, state = compute_trailing_sl(trade, ltp=107.0, activate_pct=20.0, gap_pct=10.0)
    assert new_sl == trade.current_sl == 90.0 and state == "at_initial"

    # Activate at ltp=108: candidate = 108 * 0.9 = 97.20.
    new_sl, state = compute_trailing_sl(trade, ltp=108.0, activate_pct=20.0, gap_pct=10.0)
    assert new_sl == 97.20 and state == "trailing"
    trade.current_sl = new_sl
    trade.trail_state = state

    # Move up: ltp=115 → candidate = 115 * 0.9 = 103.50.
    new_sl, state = compute_trailing_sl(trade, ltp=115.0, activate_pct=20.0, gap_pct=10.0)
    assert new_sl == 103.50 and state == "trailing"
    trade.current_sl = new_sl

    # Retrace from 115 → 104: candidate = 104 * 0.9 = 93.60, but ratchet keeps 103.50.
    new_sl, state = compute_trailing_sl(trade, ltp=104.0, activate_pct=20.0, gap_pct=10.0)
    assert new_sl == 103.50 and state == "trailing"


def test_compute_trailing_sl_put_unit(env):
    """PUT trail is symmetric: SL is above entry and moves down on retrace.
    Seed a PUT trade directly because the env fixture creates a CALL."""
    from app.models import Trade
    from app.services.trade_service import compute_trailing_sl

    with session_scope() as session:
        trade = Trade(
            underlying_key="NSE_EQ|INE002A01018",
            option_instrument_key="NSE_FO|90111",
            option_instrument_token="90111",
            tradingsymbol="RELIANCE 10 SEP 26 3000 PE",
            lot_size=1250, product="D", direction="PUT",
            entry_price=200.0, quantity=1250,
            initial_sl=220.0, current_sl=220.0,
            trail_state="at_initial", best_price=200.0, status="open",
            entry_order_id="o-x", sl_order_id="o-y",
        )
        session.add(trade)
        session.flush()
        trade_id = trade.id

    # activation_ltp = 220 * (1 - 0.20) = 176; ltp=180 NOT activated.
    with session_scope() as session:
        t = session.get(Trade, trade_id)
        new_sl, state = compute_trailing_sl(t, ltp=180.0, activate_pct=20.0, gap_pct=10.0)
        assert new_sl == 220.0 and state == "at_initial"

    # Activate: ltp=176 → candidate = 176 * 1.10 = 193.60.
    with session_scope() as session:
        t = session.get(Trade, trade_id)
        new_sl, state = compute_trailing_sl(t, ltp=176.0, activate_pct=20.0, gap_pct=10.0)
        assert new_sl == 193.60 and state == "trailing"
        t.current_sl = new_sl
        t.trail_state = state

    # Lower ltp: candidate = 160 * 1.10 = 176.00; moves down in profitable direction.
    with session_scope() as session:
        t = session.get(Trade, trade_id)
        t.current_sl = 193.60
        t.trail_state = "trailing"
        new_sl, state = compute_trailing_sl(t, ltp=160.0, activate_pct=20.0, gap_pct=10.0)
        assert new_sl == 176.00 and state == "trailing"

    # Retrace UP — SL must not move up (PUT ratchet direction is down).
    with session_scope() as session:
        t = session.get(Trade, trade_id)
        t.current_sl = 176.00
        t.trail_state = "trailing"
        new_sl, state = compute_trailing_sl(t, ltp=190.0, activate_pct=20.0, gap_pct=10.0)
        assert new_sl == 176.00 and state == "trailing"