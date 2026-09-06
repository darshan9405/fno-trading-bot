"""Instrument seeding tests — upsert/build logic with a fake master (no network)."""

import pandas as pd
import pytest
from sqlalchemy import select

from app import create_app
from app.config import Config
from app.db import dispose, session_scope
from app.models import Instrument
from app.services import instrument_service


@pytest.fixture
def env(tmp_path):
    cfg = Config()
    cfg.RATE_LIMIT_ENABLED = False
    cfg.DATABASE_URL = f"sqlite:///{tmp_path / 'seed.db'}"
    cfg.SECRET_KEY = "test-secret"
    dispose()
    create_app(cfg)
    yield
    dispose()


def _master() -> pd.DataFrame:
    return pd.DataFrame(
        [
            # EQ spot for RELIANCE + its FO lot-size row.
            {"segment": "NSE_EQ", "instrument_type": "EQ", "trading_symbol": "RELIANCE",
             "instrument_key": "NSE_EQ|RELIANCE", "exchange_token": "2885", "tick_size": 0.05,
             "underlying_symbol": "RELIANCE", "lot_size": 1250},
            {"segment": "NSE_FO", "instrument_type": "CE", "trading_symbol": "RELIANCE24OCT",
             "instrument_key": "NSE_FO|X", "instrument_token": "1", "underlying_symbol": "RELIANCE",
             "lot_size": 1250},
            # INDEX spot for NIFTY + its FO lot-size row.
            {"segment": "NSE_INDEX", "instrument_type": "INDEX", "trading_symbol": "NIFTY",
             "instrument_key": "NSE_INDEX|Nifty 50", "exchange_token": "26000", "tick_size": 0.05,
             "underlying_symbol": "NIFTY", "lot_size": 0},
            {"segment": "NSE_FO", "instrument_type": "CE", "trading_symbol": "NIFTY24OCT",
             "instrument_key": "NSE_FO|Y", "instrument_token": "2", "underlying_symbol": "NIFTY",
             "lot_size": 50},
            # EQ stock that is NOT optionable (no FO row) -> must be excluded.
            {"segment": "NSE_EQ", "instrument_type": "EQ", "trading_symbol": "SOMECO",
             "instrument_key": "NSE_EQ|SOMECO", "exchange_token": "3", "tick_size": 0.05,
             "underlying_symbol": "SOMECO", "lot_size": 1},
        ]
    )


def test_build_universe_only_optionable(env):
    uni = instrument_service.build_universe(_master())
    by = {u["symbol"]: u for u in uni}
    assert set(by) == {"RELIANCE", "NIFTY"}
    assert by["RELIANCE"]["spot_instrument_key"] == "NSE_EQ|RELIANCE"
    assert by["RELIANCE"]["segment"] == "NSE_EQ"
    assert by["RELIANCE"]["lot_size"] == 1250
    assert by["NIFTY"]["segment"] == "NSE_INDEX"
    assert by["NIFTY"]["lot_size"] == 50
    assert by["NIFTY"]["instrument_token"] == "26000"


def test_upsert_inserts_inactive_and_preserves_enabled(env):
    universe = instrument_service.build_universe(_master())
    with session_scope() as session:
        instrument_service.upsert_universe(session, universe)
    with session_scope() as session:
        rows = {i.symbol: i for i in session.execute(select(Instrument)).scalars()}
        assert set(rows) == {"RELIANCE", "NIFTY"}
        assert all(not i.enabled for i in rows.values())  # inactive by default

    # Operator enables RELIANCE from the UI; a re-seed must not flip it back.
    with session_scope() as session:
        instrument_service.set_instrument_enabled(rows["RELIANCE"].id, True)
    with session_scope() as session:
        inserted, updated = instrument_service.upsert_universe(session, instrument_service.build_universe(_master()))
    assert inserted == 0
    assert updated == 0  # idempotent: unchanged metadata
    with session_scope() as session:
        assert session.get(Instrument, rows["RELIANCE"].id).enabled is True
        assert session.get(Instrument, rows["NIFTY"].id).enabled is False

    # Updated metadata is applied but the flag still survives.
    with session_scope() as session:
        instrument_service.upsert_universe(session, [
            {**u, "lot_size": 999} for u in instrument_service.build_universe(_master())
        ])
    with session_scope() as session:
        assert session.get(Instrument, rows["RELIANCE"].id).lot_size == 999
        assert session.get(Instrument, rows["RELIANCE"].id).enabled is True


def test_seed_instruments_if_empty_noops_when_populated(env, monkeypatch):
    universe = instrument_service.build_universe(_master())
    with session_scope() as session:
        instrument_service.upsert_universe(session, universe)
    monkeypatch.setattr(instrument_service, "seed_instruments", lambda: (_ for _ in ()).throw(AssertionError("should not seed")))
    assert instrument_service.seed_instruments_if_empty() is False  # non-empty -> no network, no seeding