#!/usr/bin/env python3
"""Strategy scan across all NSE F&O optionable underlyings.

Downloads the Upstox instrument master (JSON), builds the universe of
F&O optionable stocks + indices, fetches real daily candles for each, and runs
the breakout strategy engine. Reports every signal at the last close.

Usage:
    UPSTOX_INTEGRATION_TOKEN=<token> python scripts/scan_strategy.py
    UPSTOX_INTEGRATION_TOKEN=<token> python scripts/scan_strategy.py --limit 30
    UPSTOX_INTEGRATION_TOKEN=<token> python scripts/scan_strategy.py --index-only
"""

import argparse
import os
import socket

socket.setdefaulttimeout(20)  # bound every HTTP request
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gzip
import json

import pandas as pd

from app import create_app
from app.broker import UpstoxBroker
from app.config import Config
from app.db import dispose
from app.strategy import StrategyRegistry

INSTRUMENT_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
CANDLE_DAYS = 300
SLEEP = 0.15


def load_instruments() -> pd.DataFrame:
    path = "/tmp/upstox_nse.json.gz"
    if not os.path.exists(path) or os.path.getmtime(path) < time.time() - 86400:
        import urllib.request

        print("downloading instrument master...")
        urllib.request.urlretrieve(INSTRUMENT_URL, path)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return pd.DataFrame(json.load(f))


def build_universe(df: pd.DataFrame, index_only: bool) -> list[tuple[str, str]]:
    optionable = set(
        df[(df["segment"] == "NSE_FO") & (df["instrument_type"].isin(["CE", "PE"]))]["underlying_symbol"].dropna()
    )
    uni = []
    eq = df[(df["segment"] == "NSE_EQ") & (df["instrument_type"] == "EQ")]
    idx = df[df["segment"] == "NSE_INDEX"]
    for sym in sorted(optionable):
        if index_only and sym not in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}:
            continue
        spot = eq[eq["trading_symbol"] == sym]
        if spot.empty:
            spot = idx[idx["trading_symbol"] == sym]
        if spot.empty:
            continue
        key = spot.iloc[0]["instrument_key"]
        uni.append((sym, key))
    return uni


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="cap number of underlyings scanned (0 = all)")
    parser.add_argument("--index-only", action="store_true", help="scan F&O indices only")
    args = parser.parse_args()

    token = os.getenv("UPSTOX_INTEGRATION_TOKEN")
    if not token:
        print("FATAL: set UPSTOX_INTEGRATION_TOKEN")
        return 2

    cfg = Config()
    cfg.DATABASE_URL = f"sqlite:///{tempfile.mktemp(suffix='.db')}"
    dispose()
    create_app(cfg)  # seeds strategy settings

    universe = build_universe(load_instruments(), args.index_only)
    if args.limit:
        universe = universe[: args.limit]
    print(f"Scanning {len(universe)} F&O underlyings\n")

    broker = UpstoxBroker(cfg, access_token=token)
    strategy = StrategyRegistry.get("breakout")()
    today = date.today()
    from_date = today - timedelta(days=CANDLE_DAYS)

    signals = []
    errors = 0
    for i, (symbol, key) in enumerate(universe, 1):
        try:
            candles = broker.get_historical_candles(key, "day", from_date, today)
            if candles is None or candles.empty or len(candles) < 40:
                continue
            inst = SimpleNamespace(id=i, symbol=symbol, spot_instrument_key=key)
            leads = strategy.generate(inst, candles, datetime.now(ZoneInfo("Asia/Kolkata")))
            for lead in leads:
                signals.append((symbol, lead.direction, lead.signal_type, lead.signal_level, lead.confidence))
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"  skip {symbol}: {str(e)[:80]}")
        if i % 25 == 0 or i == len(universe):
            print(f"  ... {i}/{len(universe)} scanned ({len(signals)} signals)")
        time.sleep(SLEEP)

    print("\n=== SIGNALS AT LAST CLOSE ===")
    if not signals:
        print("  (none)")
    for sym, direction, pat, level, conf in sorted(signals, key=lambda s: -s[4]):
        print(f"  {sym:<18} {direction:5s} {pat:<20} level={level:>10.2f} conf={conf:.2f}")

    print(f"\nTotal signals: {len(signals)} | underlyings: {len(universe)} | errors: {errors}")
    dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())