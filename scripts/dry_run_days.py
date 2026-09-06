#!/usr/bin/env python3
"""Realistic multi-day dry run: real strategy + real intraday paths + full trade lifecycle.

For each sampled trading day D:
  - generate leads using daily candles up to D-1 (real strategy, volume-confirmed)
  - for each lead, fetch the REAL 5-min intraday path of the underlying on day D
    and simulate the full lifecycle with the SAME rules as live:
      - entry at the breakout level when price first reaches it (10:00-14:00)
      - initial 10% stop-loss, breakeven at +5%, trail 5% from best
      - square off at 14:00

Usage:
    UPSTOX_INTEGRATION_TOKEN=<token> python scripts/dry_run_days.py
    ... --days 15 --all
    ... --days 5 --instrument NIFTY
"""

import argparse
import os
import random
import sys
import tempfile
import time
from datetime import date, datetime, time as dtime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import socket

socket.setdefaulttimeout(20)

from scan_strategy import build_universe, load_instruments  # noqa: E402

from app import create_app  # noqa: E402
from app.broker import UpstoxBroker  # noqa: E402
from app.config import Config  # noqa: E402
from app.db import dispose  # noqa: E402
from app.settings import get_setting, set_setting  # noqa: E402
from app.services import trade_service  # noqa: E402
from app.strategy import StrategyRegistry  # noqa: E402
import app.strategy.breakout as _bs  # noqa: E402

DEFAULT = ["NIFTY", "BANKNIFTY", "FINNIFTY", "RELIANCE", "KEI", "POLYCAB", "RBLBANK", "SRF", "TORNTPHARM", "MARUTI"]
FETCH_DAYS = 420
SETTING_KEYS = [
    "breakout.patterns_enabled", "breakout.min_confidence", "breakout.require_volume_spike",
    "breakout.volume_boost", "breakout.lookback_days", "breakout.swing_k", "breakout.proximity_pct",
    "breakout.min_touches", "breakout.min_trendline_points", "breakout.pole_pct",
    "breakout.volume_multiplier", "breakout.volume_window", "breakout.volume_lookback",
    "breakout.market_alignment", "initial_sl_pct", "trail_activate_pct", "trail_gap_pct",
]


def _fast_settings():
    settings = {k: get_setting(k) for k in SETTING_KEYS}
    _bs.get_setting = lambda key, default=None: settings.get(key, default)


def _resolve_picks(universe, instrument, all_universe):
    by_symbol = {s: k for s, k in universe}
    if instrument:
        if instrument not in by_symbol:
            raise SystemExit(f"FATAL: {instrument} not found in F&O universe")
        return [(instrument, by_symbol[instrument])]
    if all_universe:
        return universe
    return [(s, by_symbol[s]) for s in DEFAULT if s in by_symbol]


def _intraday_bars(broker, key, day):
    """1-minute bars for `day`, filtered to the 10:00-14:00 IST trading window."""
    df = broker.get_historical_candles(key, "1minute", day, day)
    if df is None or df.empty:
        return []
    start, end = dtime(10, 0), dtime(14, 0)
    out = []
    for ts, row in df.iterrows():
        t = ts.time()
        if start <= t < end:
            out.append({"t": t, "open": float(row["open"]), "high": float(row["high"]),
                        "low": float(row["low"]), "close": float(row["close"])})
    return out


def _simulate_trade(level, direction, bars, leverage, sl_pct, activate_pct, gap_pct):
    """Simulate the live lifecycle on the intraday path, modeling the option premium
    as: premium % move = leverage x underlying % move (ATM option approximation).
    Returns (entry_level, premium_exit, reason, trail) or None.
    """
    if direction == "CALL":
        idx = next((i for i, b in enumerate(bars) if b["high"] >= level), None)
    else:
        idx = next((i for i, b in enumerate(bars) if b["low"] <= level), None)
    if idx is None:
        return None  # price never reached the breakout level intraday
    entry = level
    base = 100.0  # normalized option premium at entry
    t = SimpleNamespace(entry_price=base, direction=direction, best_price=base,
                        current_sl=trade_service.initial_sl_for(base, direction, sl_pct),
                        trail_state="at_initial")

    def premium_of(close):
        under_ret = (close - entry) / entry if direction == "CALL" else (entry - close) / entry
        return base * (1.0 + leverage * under_ret)

    for i in range(idx, len(bars)):
        prem = premium_of(bars[i]["close"])
        if direction == "CALL":
            t.best_price = max(t.best_price, prem)
        else:
            t.best_price = min(t.best_price, prem)
        new_sl, new_state = trade_service.compute_trailing_sl(t, prem, activate_pct, gap_pct)
        if new_sl != t.current_sl:
            t.current_sl, t.trail_state = new_sl, new_state
        if trade_service.is_sl_hit(t, prem):
            reason = "trailing_sl" if t.trail_state != "at_initial" else "sl_hit"
            return entry, t.current_sl, reason, t.trail_state
    return entry, premium_of(bars[-1]["close"]), "sqoff", t.trail_state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instrument", default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--days", type=int, default=10, help="number of random trading days to sample")
    parser.add_argument("--warmup", type=int, default=70)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--leverage", type=float, default=8.0,
                        help="option premium % move per 1% underlying move (ATM approx)")
    parser.add_argument("--cost", type=float, default=0.5,
                        help="round-trip cost as %% of premium (brokerage + STT + slippage), deducted per trade")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args()

    token = os.getenv("UPSTOX_INTEGRATION_TOKEN")
    if not token:
        print("FATAL: set UPSTOX_INTEGRATION_TOKEN")
        return 2

    cfg = Config()
    cfg.DATABASE_URL = f"sqlite:///{tempfile.mktemp(suffix='.db')}"
    dispose()
    create_app(cfg)
    for kv in args.set:
        key, _, value = kv.partition("=")
        set_setting(key, value if value.lower() not in ("true", "false") else value.lower() == "true")
    _fast_settings()

    picks = _resolve_picks(build_universe(load_instruments(), index_only=False), args.instrument, args.all)
    broker = UpstoxBroker(cfg, access_token=token)
    strategy = StrategyRegistry.get("breakout")()
    today = date.today()

    sl_pct = float(get_setting("initial_sl_pct", 10.0))
    activate_pct = float(get_setting("trail_activate_pct", 5.0))
    gap_pct = float(get_setting("trail_gap_pct", 5.0))
    alignment = str(get_setting("breakout.market_alignment", "off"))

    print(f"Dry-run days: {len(picks)} underlyings, {args.days} random days (10:00-14:00, SL {sl_pct:.0f}%, "
          f"trail {gap_pct:.0f}%, sqoff 14:00, option leverage x{args.leverage:.0f}, "
          f"cost {args.cost:.1f}%, alignment={alignment})\n")

    daily = {}
    for symbol, key in picks:
        df = broker.get_historical_candles(key, "day", today - timedelta(days=FETCH_DAYS), today)
        if df is not None and not df.empty and len(df) >= args.warmup + 2:
            daily[symbol] = (key, df)
        time.sleep(0.05)

    # NIFTY daily series for the market-alignment filter.
    nifty_df = None
    if alignment != "off":
        for sym, (k, df) in daily.items():
            if sym == "NIFTY":
                nifty_df = df
                break
        if nifty_df is None:
            nifty_df = broker.get_historical_candles("NSE_INDEX|Nifty 50", "day", today - timedelta(days=FETCH_DAYS), today)

    ref = max((df for _, df in daily.values()), key=len).index
    valid = list(range(args.warmup, len(ref) - 1))
    sampled = sorted(random.Random(args.seed).sample(valid, min(args.days, len(valid))))

    all_rows = []
    for ridx in sampled:
        d = ref[ridx]
        leads_today = []
        for symbol, (key, df) in daily.items():
            if d not in df.index:
                continue
            pos = df.index.get_indexer([d])[0]
            fed = df.iloc[:pos]
            try:
                leads = strategy.generate(SimpleNamespace(id=0, symbol=symbol, spot_instrument_key=key), fed, None)
            except Exception:
                continue
            if leads:
                signal_date = fed.index[-1]
                leads_today.append((symbol, key, leads[0], signal_date))
        if not leads_today:
            continue

        print(f"--- DAY {d.date()} --- {len(leads_today)} lead(s)")
        for symbol, key, lead, signal_date in leads_today:
            if alignment != "off" and nifty_df is not None:
                from app.strategy.breakout.market import is_aligned, trend_on_date

                trend = trend_on_date(nifty_df, signal_date)
                if not is_aligned(lead.direction, trend):
                    continue  # counter-trend -> skip
            try:
                bars = _intraday_bars(broker, key, d.date())
                result = _simulate_trade(lead.signal_level, lead.direction, bars, args.leverage, sl_pct, activate_pct, gap_pct)
            except Exception as e:  # noqa: BLE001
                print(f"    {symbol:<14} intraday fetch failed ({str(e)[:50]})")
                continue
            if result is None:
                print(f"    {symbol:<14} {lead.direction:5s} {lead.signal_type:<18} level={lead.signal_level:>9.2f} "
                      f"-> NO INTRADAY TOUCH (skipped)")
                continue
            entry, exit_prem, reason, trail = result
            gross = (exit_prem - 100.0) / 100.0 if lead.direction == "CALL" else (100.0 - exit_prem) / 100.0
            pnl = gross - args.cost / 100.0  # net of round-trip costs
            all_rows.append((symbol, lead.direction, lead.signal_type, lead.signal_level, reason, pnl, trail))
            print(f"    {symbol:<14} {lead.direction:5s} {lead.signal_type:<18} level={entry:>9.2f} -> premium {exit_prem:>7.2f} "
                  f"[{reason} / {trail}] pnl {100*pnl:+.2f}%")
        time.sleep(0.1)

    print("\n=== AGGREGATE ===")
    if not all_rows:
        print("  (no traded leads)")
    else:
        rets = [r[5] for r in all_rows]
        wins = sum(1 for r in rets if r > 0)
        reasons = {}
        for r in all_rows:
            reasons[r[4]] = reasons.get(r[4], 0) + 1
        print(f"Traded: {len(rets)} | Win rate: {wins}/{len(rets)} ({100*wins/len(rets):.1f}%)")
        print(f"Avg pnl/trade: {100*sum(rets)/len(rets):+.2f}% | Total (sum): {100*sum(rets):+.2f}%")
        print(f"Best: {100*max(rets):+.2f}% | Worst: {100*min(rets):+.2f}%")
        print(f"Exits: {reasons}")
        from collections import defaultdict

        bypat = defaultdict(list)
        for r in all_rows:
            bypat[r[2]].append(r[5])
        print("\nBY PATTERN:")
        for pat, r in sorted(bypat.items()):
            print(f"  {pat:<18} n={len(r):<4} win={100*sum(1 for x in r if x>0)/len(r):.0f}% avg={100*sum(r)/len(r):+.2f}%")

    dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())