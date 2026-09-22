#!/usr/bin/env python3
"""Pre-market dry-run: simulate a full trading day against the real scheduler code.

Drives S1 (lead generator) -> S3 (order placer) -> S2 (trade tracker) with a
simulated weekday clock and a test-only SimBroker (scripted fills). Validates
the whole pipeline offline — no real orders.

The LLM breakout detector is stubbed via `app.strategy.llm_breakout.StubClient`
so the dry run has no external API dependency. The stub is wired in by
monkey-patching the strategy's default client builder before the lead
generator instantiates the strategy.

Usage:
    python scripts/dry_run.py --scenario profit      # price rises -> trailing -> 14:00 square-off
    python scripts/dry_run.py --scenario sl_hit     # price falls -> stop-loss hit
"""

import argparse
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

from sim_broker import SimBroker
from app import create_app
from app.broker.base import InstrumentView
from app.config import Config
from app.db import dispose, session_scope
from app.models import Instrument, Lead, Trade
from app.scheduler.lead_generator import run_lead_generator
from app.scheduler.order_placer import run_order_placer
from app.scheduler.trade_tracker import run_trade_tracker
from app.settings import set_setting
from app.strategy.llm_breakout import StubClient

IST = ZoneInfo("Asia/Kolkata")
DAY = datetime(2026, 9, 7, 0, 0, tzinfo=IST)  # Monday


def _now(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


def _range_candles(underlying: str) -> pd.DataFrame:
    """260 bars: long 95-105 range + today's close just above 105 -> CALL breakout.

    The LLM stub below responds with a horizontal_range CALL signal at 105.0.
    """
    n = 260
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    highs = [105.0] * (n - 1) + [106.0]
    lows = [95.0] * (n - 1) + [100.0]
    closes = [100.0] * (n - 1) + [105.2]
    opens = [100.0] * (n - 1) + [105.0]
    volume = [1000.0] * n
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volume},
        index=idx,
    )


def _option_path(scenario: str):
    """Return a function (minute_of_day) -> option LTP."""
    def profit(m):
        return 100.0 + max(0.0, m - 605) * 0.06  # ~100 -> ~112 by 14:00

    def sl_hit(m):
        return 100.0 if m < 610 else 88.0  # crash after 10:10

    return profit if scenario == "profit" else sl_hit


def _install_llm_stub() -> None:
    """Patch the strategy's default-client builder so it returns our StubClient.

    The strategy instantiates its client lazily inside `generate()`, so this
    patch is picked up by the next call.
    """
    import app.strategy.llm_breakout.client as _client_mod

    stub = StubClient(responses=[
        {"signals": [{
            "direction": "CALL",
            "pattern_type": "horizontal_range",
            "trigger_price": 105.0,
            "confidence": 0.8,
            "rationale": "dry_run fixture: horizontal_range CALL",
        }]}
    ])
    _client_mod.build_default_client = lambda: stub  # type: ignore[assignment]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=["profit", "sl_hit"], default="profit")
    args = parser.parse_args()

    cfg = Config()
    cfg.DATABASE_URL = f"sqlite:///{tempfile.mktemp(suffix='.db')}"
    dispose()
    create_app(cfg)

    _install_llm_stub()

    set_setting("strategy", "llm_breakout")
    set_setting("trading_start", "10:00")
    set_setting("trade_end_time", "11:00")
    set_setting("sqoff_time", "14:00")
    set_setting("llm.enabled", True)
    set_setting("llm.min_confidence", 0.6)
    set_setting("llm.lookback_candles", 250)
    set_setting("max_lead_price_divergence_pct", 0.5)
    set_setting("market_calendar_last_sync_date", "2026-09-07")

    underlying = "NSE_INDEX|Nifty 50"
    expiry = date.today() + timedelta(days=30)
    with session_scope() as s:
        s.add(Instrument(symbol="NIFTY", exchange="NSE", segment="NSE_INDEX",
                         spot_instrument_key=underlying, instrument_token="26000",
                         trading_symbol="NIFTY", lot_size=50, enabled=True))

    broker = SimBroker(
        candles={underlying: _range_candles(underlying)},
        contracts=[InstrumentView(
            instrument_key="NSE_FO|84123", trading_symbol="NIFTY 30 OCT 26 105 CE",
            instrument_type="CE", expiry=expiry, strike_price=105.0,
            lot_size=50, underlying_key=underlying)],
        expiries=[expiry],
    )

    print(f"Dry run — scenario: {args.scenario} (simulated {DAY:%a %d %b %Y})\n")

    broker.set_ltps({underlying: 105.0, "NSE_FO|84123": 100.0})
    run_lead_generator(broker=broker, now=_now(10, 0))
    run_order_placer(broker=broker, now=_now(10, 5))

    with session_scope() as s:
        leads = list(s.execute(__import__("sqlalchemy").select(Lead)).scalars())
        trades = list(s.execute(__import__("sqlalchemy").select(Trade)).scalars())
    print(f"Leads: {len(leads)}  |  Trades opened: {len(trades)}")
    for l in leads:
        print(f"  lead: {l.direction} {l.signal_type} @ {l.signal_level} conf={l.confidence} status={l.status}")
    for t in trades:
        print(f"  trade: {t.tradingsymbol} entry={t.entry_price} sl={t.initial_sl} qty={t.quantity}")

    path = _option_path(args.scenario)
    for minute in range(610, 841, 5):  # 10:10 -> 14:00
        hh, mm = divmod(minute, 60)
        broker.set_ltps({"NSE_FO|84123": path(minute)})
        run_trade_tracker(broker=broker, now=_now(hh, mm))

    run_trade_tracker(broker=broker, now=_now(14, 0))

    with session_scope() as s:
        trades = list(s.execute(__import__("sqlalchemy").select(Trade)).scalars())

    print("\nFinal trades:")
    for t in trades:
        print(f"  {t.tradingsymbol} {t.direction}: entry={t.entry_price} status={t.status} "
              f"exit={t.exit_price} reason={t.exit_reason} pnl={t.realized_pnl} "
              f"trail={t.trail_state} (sl moved: {len(broker.modified)} times)")

    total = sum(t.realized_pnl or 0 for t in trades)
    print(f"\nNet realized P&L: {total:.2f}")
    print(f"Orders placed: {len(broker.placed)} | SL modifications: {len(broker.modified)}")

    dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
