"""Instrument whitelist: seeding from the Upstox instrument master + enable/disable.

Seeded rows are inserted with `enabled=False`; the operator enables the
underlyings they actually want to trade from the UI. Re-seeding preserves the
operator's enabled flags (an update never re-disables a choice).
"""

import gzip
import json
import logging
import os
import socket
import time
import urllib.request

import pandas as pd
from sqlalchemy import func, select

from app.db import session_scope
from app.models import Instrument

log = logging.getLogger(__name__)

INSTRUMENT_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
MASTER_PATH = "/tmp/upstox_nse.json.gz"
MASTER_TTL_SECONDS = 86400  # re-download the (large) master at most once a day


def fetch_master() -> pd.DataFrame:
    """Download (with a 1-day local cache) and parse the NSE instrument master."""
    socket.setdefaulttimeout(20)  # bound every HTTP request
    if not os.path.exists(MASTER_PATH) or os.path.getmtime(MASTER_PATH) < time.time() - MASTER_TTL_SECONDS:
        log.info("downloading instrument master from Upstox")
        urllib.request.urlretrieve(INSTRUMENT_URL, MASTER_PATH)
    with gzip.open(MASTER_PATH, "rt", encoding="utf-8") as f:
        return pd.DataFrame(json.load(f))


def build_universe(df: pd.DataFrame) -> list[dict]:
    """Every F&O optionable underlying -> row payload for the instruments table."""
    optionable = set(
        df[(df["segment"] == "NSE_FO") & (df["instrument_type"].isin(["CE", "PE"]))]["underlying_symbol"].dropna()
    )
    eq = df[(df["segment"] == "NSE_EQ") & (df["instrument_type"] == "EQ")]
    idx = df[df["segment"] == "NSE_INDEX"]
    lots = df[df["segment"] == "NSE_FO"].groupby("underlying_symbol")["lot_size"].max().to_dict()

    universe = []
    for sym in sorted(optionable):
        spot = eq[eq["trading_symbol"] == sym]
        if spot.empty:
            spot = idx[idx["trading_symbol"] == sym]
        if spot.empty:
            continue
        row = spot.iloc[0]
        ts = row.get("tick_size")
        tick_size = 0.05 if pd.isna(ts) else float(ts)
        lot = lots.get(sym)
        lot_size = 1 if lot is None or pd.isna(lot) else int(lot)
        universe.append(
            {
                "symbol": sym,
                "exchange": "NSE",
                "segment": row["segment"],
                "spot_instrument_key": row["instrument_key"],
                "instrument_token": str(row["exchange_token"]),
                "trading_symbol": row["trading_symbol"],
                "lot_size": lot_size,
                "tick_size": tick_size,
                "chart_interval": "day",
            }
        )
    return universe


def upsert_universe(session, universe: list[dict]) -> tuple[int, int]:
    """Insert missing instruments (enabled=False); update metadata on existing
    rows while preserving their enabled flag. Returns (inserted, updated)."""
    existing = {i.spot_instrument_key: i for i in session.execute(select(Instrument)).scalars()}
    inserted = updated = 0
    for u in universe:
        inst = existing.get(u["spot_instrument_key"])
        if inst is None:
            session.add(Instrument(enabled=False, **u))
            inserted += 1
        else:
            dirty = False
            for field, value in u.items():
                if getattr(inst, field) != value:
                    setattr(inst, field, value)
                    dirty = True
            if dirty:
                updated += 1
    return inserted, updated


def seed_instruments() -> tuple[int, int]:
    """Fetch the master and upsert the universe. Idempotent; never disables."""
    df = fetch_master()
    universe = build_universe(df)
    with session_scope() as session:
        inserted, updated = upsert_universe(session, universe)
    log.info("instrument seed: %d inserted, %d updated (%d optionable underlyings)", inserted, updated, len(universe))
    return inserted, updated


def seed_instruments_if_empty() -> bool:
    """Safety net: seed only when the table is empty (S1 uses this)."""
    with session_scope() as session:
        count = session.execute(select(func.count()).select_from(Instrument)).scalar()
    if count == 0:
        seed_instruments()
        return True
    return False


def list_instruments() -> list[dict]:
    with session_scope() as session:
        rows = session.execute(select(Instrument).order_by(Instrument.segment, Instrument.symbol)).scalars()
        return [_serialize(i) for i in rows]


def set_instrument_enabled(instrument_id: int, enabled: bool) -> dict | None:
    with session_scope() as session:
        inst = session.get(Instrument, instrument_id)
        if inst is None:
            return None
        inst.enabled = bool(enabled)
        return _serialize(inst)


def _serialize(i: Instrument) -> dict:
    return {
        "id": i.id,
        "symbol": i.symbol,
        "exchange": i.exchange,
        "segment": i.segment,
        "spot_instrument_key": i.spot_instrument_key,
        "instrument_token": i.instrument_token,
        "trading_symbol": i.trading_symbol,
        "lot_size": i.lot_size,
        "tick_size": i.tick_size,
        "chart_interval": i.chart_interval,
        "enabled": i.enabled,
    }