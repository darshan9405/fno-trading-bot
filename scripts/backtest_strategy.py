#!/usr/bin/env python3
"""Backtest the breakout strategy on real Upstox daily candles.

Two modes:

1. Consecutive (default): for each day D in a window, feed the strategy candles
   up to D-1 and evaluate day D's open->close move in the signal direction.

2. Random-days sampling (--random-days N): sample N random trading days; for
   each sampled day, generate ALL leads across the market (every underlying,
   using candles up to the prior close) and evaluate the sampled day's outcome
   — i.e. "what leads fire that day, and what happens if we trade them".

No look-ahead: signals are always based on the previous close.

Usage:
    UPSTOX_INTEGRATION_TOKEN=<token> python scripts/backtest_strategy.py
    ... --random-days 40 --all
    ... --random-days 20 --instrument NIFTY
"""

import argparse
import os
import random
import sys
import tempfile
import time
from datetime import date, timedelta
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
from app.strategy import StrategyRegistry  # noqa: E402
import app.strategy.breakout as _bs  # noqa: E402

DEFAULT = ["NIFTY", "BANKNIFTY", "FINNIFTY", "RELIANCE", "KEI", "POLYCAB", "RBLBANK", "SRF", "TORNTPHARM", "MARUTI"]
FETCH_DAYS = 420
SETTING_KEYS = [
    "breakout.patterns_enabled", "breakout.min_confidence", "breakout.require_volume_spike",
    "breakout.volume_boost", "breakout.lookback_days", "breakout.swing_k", "breakout.proximity_pct",
    "breakout.min_touches", "breakout.min_trendline_points", "breakout.pole_pct",
    "breakout.volume_multiplier", "breakout.volume_window", "breakout.volume_lookback",
]


def _coerce(value: str):
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _fast_settings() -> None:
    """Pre-read settings once and bypass DB on every generate() call (perf)."""
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


def _outcome(row, direction):
    o, c = float(row["open"]), float(row["close"])
    if o <= 0:
        return None
    return (c - o) / o if direction == "CALL" else (o - c) / o


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instrument", default=None)
    parser.add_argument("--all", action="store_true", help="scan all F&O underlyings")
    parser.add_argument("--days", type=int, default=90, help="consecutive backtest window")
    parser.add_argument("--random-days", type=int, default=0, help="sample N random trading days (cross-sectional)")
    parser.add_argument("--warmup", type=int, default=70)
    parser.add_argument("--seed", type=int, default=42)
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
        set_setting(key, _coerce(value))
        print(f"override: {key} = {value}")
    _fast_settings()

    picks = _resolve_picks(build_universe(load_instruments(), index_only=False), args.instrument, args.all)
    broker = UpstoxBroker(cfg, access_token=token)
    strategy = StrategyRegistry.get("breakout")()
    today = date.today()

    if args.random_days:
        _run_random_days(broker, strategy, picks, args.random_days, args.warmup, args.seed, today)
    else:
        _run_consecutive(broker, strategy, picks, args.days, args.warmup, today)
    dispose()
    return 0


def _report(rows):
    if not rows:
        print("\n  (no signals)")
        return
    rets = [r[-1] for r in rows]
    wins = sum(1 for r in rets if r > 0)
    print("\n=== SUMMARY ===")
    print(f"Signals: {len(rets)} | Win rate: {wins}/{len(rets)} ({100*wins/len(rets):.1f}%)")
    print(f"Avg return: {100*sum(rets)/len(rets):.2f}% | Total (sum): {100*sum(rets):.2f}%")
    print(f"Best: {100*max(rets):.2f}% | Worst: {100*min(rets):.2f}%")
    from collections import defaultdict

    bypat = defaultdict(list)
    for r in rows:
        bypat[r[2]].append(r[-1])
    print("\n=== BY PATTERN ===")
    for pat, r in sorted(bypat.items()):
        print(f"  {pat:<18} n={len(r):<4} win={100*sum(1 for x in r if x>0)/len(r):.0f}% avg={100*sum(r)/len(r):+.2f}%")


def _run_consecutive(broker, strategy, picks, days, warmup, today):
    print(f"Consecutive backtest: {len(picks)} underlyings, window {days} days\n")
    rows = []
    for symbol, key in picks:
        df = broker.get_historical_candles(key, "day", today - timedelta(days=FETCH_DAYS), today)
        if df is None or df.empty:
            continue
        inst = SimpleNamespace(id=0, symbol=symbol, spot_instrument_key=key)
        for i in range(warmup, len(df) - 1):
            try:
                leads = strategy.generate(inst, df.iloc[:i], None)
            except Exception:
                continue
            if not leads:
                continue
            lead = leads[0]
            ret = _outcome(df.iloc[i], lead.direction)
            if ret is not None:
                rows.append((symbol, lead.direction, lead.signal_type, df.index[i - 1], df.index[i], ret))
    _report(rows)


def _run_random_days(broker, strategy, picks, n_days, warmup, seed, today):
    print(f"Random-day sampling: {len(picks)} underlyings, {n_days} random days\n")
    data = {}
    for symbol, key in picks:
        df = broker.get_historical_candles(key, "day", today - timedelta(days=FETCH_DAYS), today)
        if df is not None and not df.empty and len(df) >= warmup + 2:
            data[symbol] = (key, df)
        time.sleep(0.05)

    ref = max((df for _, df in data.values()), key=len).index
    valid = list(range(warmup, len(ref) - 1))
    sampled = sorted(random.Random(seed).sample(valid, min(n_days, len(valid))))
    print(f"Sampled {len(sampled)} days from {ref[0].date()}..{ref[-1].date()}\n")

    all_rows = []
    for ridx in sampled:
        d = ref[ridx]
        day_rows = []
        for symbol, (key, df) in data.items():
            if d not in df.index:
                continue
            pos = df.index.get_indexer([d])[0]
            inst = SimpleNamespace(id=0, symbol=symbol, spot_instrument_key=key)
            try:
                leads = strategy.generate(inst, df.iloc[:pos], None)
            except Exception:
                continue
            if not leads:
                continue
            lead = leads[0]
            ret = _outcome(df.iloc[pos], lead.direction)
            if ret is not None:
                day_rows.append((symbol, lead.direction, lead.signal_type, lead.signal_level, ret))
        if not day_rows:
            continue
        rets = [r[4] for r in day_rows]
        day_win = 100 * sum(1 for x in rets if x > 0) / len(rets)
        day_avg = 100 * sum(rets) / len(rets)
        print(f"DAY {d} — {len(day_rows)} lead(s), win {day_win:.0f}%, avg {day_avg:+.2f}%")
        for sym, dr, pat, lvl, ret in day_rows:
            print(f"    {sym:<14} {dr:5s} {pat:<18} level={lvl:>9.2f} -> {100*ret:+.2f}%")
        all_rows.extend(day_rows)

    _report(all_rows)

if __name__ == "__main__":
    sys.exit(main())
