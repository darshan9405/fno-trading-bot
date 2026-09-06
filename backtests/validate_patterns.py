"""Breakout pattern validation.

Runs the breakout strategy over daily candles and reports detected signals.

Usage:
    python backtests/validate_patterns.py                      # synthetic demo data
    python backtests/validate_patterns.py data.csv             # your own OHLC CSV
    UPSTOX_INTEGRATION_TOKEN=... python backtests/validate_patterns.py --upstox --instrument "NSE_INDEX|Nifty 50"

CSV columns: date,open,high,low,close (volume optional). One row per day.
"""

from datetime import date, timedelta

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from app.strategy.breakout.detector import DEFAULT_PATTERNS, run_detectors


def synthetic() -> pd.DataFrame:
    """Assemble a few embedded patterns into one series (best-effort demo)."""
    idx = pd.date_range("2025-01-01", periods=200, freq="D")
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 0.4, len(idx)))
    # embed a horizontal range in the last 60 bars
    base = float(close[-61])
    close[-61:] = np.linspace(base, base, 61)
    close[-1] = close[-2] + 0.6  # today: pokes above the range
    high = np.maximum(close + np.abs(rng.normal(0, 0.3, len(idx))), close + 0.3)
    low = np.minimum(close - np.abs(rng.normal(0, 0.3, len(idx))), close - 0.3)
    open_ = close - rng.normal(0, 0.2, len(idx))
    # pin the range: last 60 bars bounded
    rng_hi = max(high[-61:-1])
    high[-61:-1] = rng_hi
    low[-61:-1] = rng_hi - 6.0
    close[-61:-1] = (high[-61:-1] + low[-61:-1]) / 2
    high[-1] = rng_hi + 1.0
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
         "volume": rng.integers(1_000, 10_000, len(idx)).astype(float), "oi": 1e6},
        index=idx,
    )


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()


def load_upstox(instrument_key: str, days: int = 300) -> pd.DataFrame:
    """Fetch real daily candles from the Upstox production API (read-only).

    Requires UPSTOX_INTEGRATION_TOKEN in the environment.
    """
    import os

    from app.broker import UpstoxBroker
    from app.config import Config

    token = os.getenv("UPSTOX_INTEGRATION_TOKEN")
    if not token:
        raise SystemExit("set UPSTOX_INTEGRATION_TOKEN to fetch candles from Upstox")
    broker = UpstoxBroker(Config(), access_token=token)
    return broker.get_historical_candles(instrument_key, "day", date.today() - timedelta(days=days), date.today())


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Validate breakout detection on candles.")
    parser.add_argument("csv", nargs="?", help="optional OHLC CSV (date,open,high,low,close[,volume])")
    parser.add_argument("--upstox", action="store_true", help="fetch real candles from the Upstox production API")
    parser.add_argument("--instrument", default="NSE_INDEX|Nifty 50", help="Upstox instrument key (with --upstox)")
    parser.add_argument("--days", type=int, default=300, help="lookback days (with --upstox)")
    args = parser.parse_args()

    if args.upstox:
        df = load_upstox(args.instrument, args.days)
        print(f"Fetched {len(df)} daily bars for {args.instrument} (last: {df.index[-1].date()})")
    elif args.csv:
        df = load_csv(args.csv)
        print(f"Loaded {len(df)} rows from {args.csv}")
    else:
        df = synthetic()
        print(f"Generated {len(df)} synthetic daily bars")

    signals = run_detectors(df, DEFAULT_PATTERNS)
    print(f"\nDetected {len(signals)} signal(s) on the last bar ({df.index[-1].date()}):")
    if not signals:
        print("  (none)")
    for s in signals:
        print(f"  {s.direction:5s} {s.signal_type:20s} level={s.signal_level:>10.2f} conf={s.confidence:.2f}")
    print(f"\nLast close: {float(df['close'].iloc[-1]):.2f}")


if __name__ == "__main__":
    main()