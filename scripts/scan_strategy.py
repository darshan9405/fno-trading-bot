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
import logging
import os
import random
import socket
import threading

socket.setdefaulttimeout(20)  # bound every HTTP request
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from app.settings import get_setting
from app.strategy import StrategyRegistry

INSTRUMENT_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
CANDLE_DAYS = 300
SLEEP = 0.15

log = logging.getLogger("scan_strategy")


def _setup_logging(verbose: bool) -> None:
    """Single-line-per-event logging to stderr (so stdout stays clean for
    the final signal table). Verbose adds DEBUG-level chatter (per-instrument
    candle fetches, raw LLM payloads, and model reasoning); default is INFO."""
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    handler.terminator = "\n"
    log.setLevel(level)
    log.handlers.clear()
    log.addHandler(handler)
    log.propagate = False
    # In --verbose mode, also surface DEBUG from the LLM client + detector so
    # the raw request/response payload (incl. reasoning_content) is visible.
    if verbose:
        for name in ("app.strategy.llm_breakout.client",
                     "app.strategy.llm_breakout.detector"):
            lg = logging.getLogger(name)
            lg.setLevel(logging.DEBUG)
            lg.handlers.clear()
            lg.addHandler(handler)
            lg.propagate = False


def load_instruments() -> pd.DataFrame:
    path = "/tmp/upstox_nse.json.gz"
    if not os.path.exists(path) or os.path.getmtime(path) < time.time() - 86400:
        import urllib.request

        log.info("downloading instrument master from %s -> %s", INSTRUMENT_URL, path)
        t0 = time.monotonic()
        urllib.request.urlretrieve(INSTRUMENT_URL, path)
        log.info("instrument master downloaded in %.1fs", time.monotonic() - t0)
    else:
        age_h = (time.time() - os.path.getmtime(path)) / 3600
        log.info("reusing cached instrument master (%s, %.1fh old)", path, age_h)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        df = pd.DataFrame(json.load(f))
    log.info("instrument master loaded: %d rows", len(df))
    return df


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


def _fmt_eta(elapsed_s: float, scanned: int, total: int) -> str:
    if scanned <= 0:
        return "ETA ?"
    rate = scanned / elapsed_s
    remaining = (total - scanned) / rate if rate > 0 else 0
    return f"{elapsed_s:.1f}s elapsed, {rate:.2f} stocks/s, ETA {remaining:.0f}s"


def _scan_one(symbol, key, idx, broker, strategy_cls, from_date, today, now):
    """Worker entrypoint. Returns (idx, symbol, candidates, error)."""
    t0 = time.monotonic()
    try:
        strategy = strategy_cls()
        candles = broker.get_historical_candles(key, "day", from_date, today)
        candle_ms = (time.monotonic() - t0) * 1000
        if candles is None or candles.empty:
            return (idx, symbol, [], None, candle_ms, 0.0)
        if len(candles) < 40:
            return (idx, symbol, [], None, candle_ms, 0.0)
        t1 = time.monotonic()
        leads = strategy.generate(
            SimpleNamespace(id=idx, symbol=symbol, spot_instrument_key=key),
            candles, now,
        )
        llm_ms = (time.monotonic() - t1) * 1000
        return (idx, symbol, list(leads), None, candle_ms, llm_ms)
    except Exception as e:
        return (idx, symbol, [], (str(e), traceback.format_exc()),
                (time.monotonic() - t0) * 1000, 0.0)


def _print_lead(symbol, lead):
    return (f"{lead.direction}/{lead.signal_type}@{lead.signal_level:.2f}"
            f"(conf={lead.confidence:.2f})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="cap number of underlyings scanned (0 = all)")
    parser.add_argument("--index-only", action="store_true", help="scan F&O indices only")
    parser.add_argument("--max-calls", type=int, default=0,
                        help="override llm.max_calls_per_run (0 = use DB setting)")
    parser.add_argument("--random-sample", type=int, default=0,
                        help="pick N random underlyings (0 = use full list, in order)")
    parser.add_argument("--seed", type=int, default=None,
                        help="random seed for --random-sample (default: time-based)")
    parser.add_argument("--max-leads", type=int, default=0,
                        help="stop scanning once this many leads are collected (0 = unlimited)")
    parser.add_argument("--symbol", type=str, default="",
                        help="scan only this underlying symbol (e.g. PATANJALI); "
                             "must be F&O optionable. Overrides --random-sample/--limit.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="DEBUG-level logging (per-instrument candle fetch + LLM call timing)")
    parser.add_argument("--max-workers", type=int, default=4,
                        help="concurrent worker threads (default 4)")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    token = os.getenv("UPSTOX_INTEGRATION_TOKEN")
    if not token:
        log.error("UPSTOX_INTEGRATION_TOKEN env var is not set")
        return 2

    log.info("initialising app (temp sqlite, seeds strategy settings)")
    cfg = Config()
    cfg.DATABASE_URL = f"sqlite:///{tempfile.mktemp(suffix='.db')}"
    dispose()
    create_app(cfg)

    log.info("building F&O universe from instrument master")
    universe = build_universe(load_instruments(), args.index_only)
    if args.symbol:
        wanted = args.symbol.upper()
        before = len(universe)
        universe = [(s, k) for s, k in universe if s == wanted]
        if not universe:
            log.error("--symbol %r not in F&O universe (was %d candidates)", wanted, before)
            return 3
        log.info("--symbol=%s -> 1 underlying", wanted)
    elif args.random_sample:
        rng = random.Random(args.seed)
        universe = rng.sample(universe, k=min(args.random_sample, len(universe)))
        log.info("random-sample=%d seed=%s -> %d underlyings", args.random_sample, args.seed, len(universe))
    elif args.limit:
        universe = universe[: args.limit]
        log.info("--limit=%d -> %d underlyings", args.limit, len(universe))

    log.info("constructing Upstox broker")
    broker = UpstoxBroker(cfg, access_token=token)
    strategy_name = get_setting("strategy", "llm_breakout")
    strategy = StrategyRegistry.get(strategy_name)()
    cap = args.max_calls or int(get_setting("llm.max_calls_per_run", 50))
    strategy.begin_run(cap)
    today = date.today()
    from_date = today - timedelta(days=CANDLE_DAYS)
    log.info("strategy=%s max_calls=%d max_leads=%s universe=%d workers=%d",
             strategy_name, cap, args.max_leads or "unlimited", len(universe), args.max_workers)
    print(f"Scanning {len(universe)} F&O underlyings (strategy={strategy_name}, max_calls={cap}, "
          f"max_leads={args.max_leads or 'unlimited'}, seed={args.seed}, workers={args.max_workers})")
    print(f"  candles: {CANDLE_DAYS} days ending {today}")
    print(f"  verbose={'on' if args.verbose else 'off'}\n", file=sys.stderr)

    signals = []
    errors = 0
    scanned = 0
    started = time.monotonic()
    last_progress = started
    PROGRESS_INTERVAL_S = 5.0
    cap_lock = threading.Lock()

    run_now = datetime.now(ZoneInfo("Asia/Kolkata"))

    with ThreadPoolExecutor(max_workers=args.max_workers,
                            thread_name_prefix="scan") as pool:
        futures = {
            pool.submit(_scan_one, sym, key, i, broker, StrategyRegistry.get(strategy_name),
                        from_date, today, run_now): i
            for i, (sym, key) in enumerate(universe, 1)
        }
        try:
            for fut in as_completed(futures):
                idx, symbol, leads, err, candle_ms, llm_ms = fut.result()
                scanned += 1
                if err is not None:
                    errors += 1
                    log.warning("[%d/%d] %s: ERROR %s",
                                idx, len(universe), symbol, err[0][:120])
                else:
                    new_leads = []
                    for lead in leads:
                        signals.append((symbol, lead.direction, lead.signal_type,
                                        lead.signal_level, lead.confidence))
                        new_leads.append(_print_lead(symbol, lead))
                    total_ms = candle_ms + llm_ms
                    if new_leads:
                        log.info("[%d/%d] %s: %.0fms total (candles %.0fms + %s %.0fms) -> %d lead(s): %s",
                                 idx, len(universe), symbol, total_ms, candle_ms,
                                 strategy_name, llm_ms, len(new_leads), ", ".join(new_leads))
                    elif args.verbose:
                        log.debug("[%d/%d] %s: %.0fms total (candles %.0fms + %s %.0fms), no leads",
                                  idx, len(universe), symbol, total_ms, candle_ms,
                                  strategy_name, llm_ms)

                now_ts = time.monotonic()
                if now_ts - last_progress >= PROGRESS_INTERVAL_S or scanned == len(universe):
                    elapsed = now_ts - started
                    log.info("progress %d/%d scanned, %d signals, %d errors, %s",
                             scanned, len(universe), len(signals), errors,
                             _fmt_eta(elapsed, scanned, len(universe)))
                    last_progress = now_ts

                with cap_lock:
                    if args.max_leads and len(signals) >= args.max_leads:
                        elapsed = time.monotonic() - started
                        log.info("hit --max-leads=%d after %d underlyings (%.1fs), stopping early",
                                 args.max_leads, scanned, elapsed)
                        # Cancel any in-flight tasks.
                        for f in futures:
                            if not f.done():
                                f.cancel()
                        break
        finally:
            # Best-effort cleanup if we exited without hitting the cap.
            pass

    print("\n=== SIGNALS AT LAST CLOSE ===")
    if not signals:
        print("  (none)")
    for sym, direction, pat, level, conf in sorted(signals, key=lambda s: -s[4]):
        print(f"  {sym:<18} {direction:5s} {pat:<20} level={level:>10.2f} conf={conf:.2f}")

    elapsed = time.monotonic() - started
    summary = (f"Total signals: {len(signals)} | underlyings scanned: {scanned}/{len(universe)} "
               f"| errors: {errors} | elapsed: {elapsed:.1f}s")
    log.info("done. %s", summary.replace("Total signals: ", "").replace(" | ", ", "))
    print(summary)
    dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())