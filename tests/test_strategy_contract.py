"""Contract test for the lead-generator Strategy interface.

Every registered strategy must accept (instrument, candles, now) and return a
list of valid LeadCandidate objects. A future strategy that violates the
contract fails here immediately.
"""

import numpy as np
import pandas as pd
import pytest
from types import SimpleNamespace

from app.strategy import StrategyRegistry
from app.strategy.base import LeadCandidate


def _synthetic_candles(n=150, seed=42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    high = close + np.abs(rng.normal(0, 0.3, n))
    low = close - np.abs(rng.normal(0, 0.3, n))
    return pd.DataFrame(
        {
            "open": close - rng.normal(0, 0.2, n),
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.integers(1_000, 10_000, n).astype(float),
        }
    )


def _fake_instrument():
    return SimpleNamespace(id=1, symbol="NIFTY", spot_instrument_key="NSE_INDEX|Nifty 50")


@pytest.fixture
def db_env(tmp_path):
    """DB with seeded settings (breakout strategy reads settings table)."""
    from app import create_app
    from app.config import Config
    from app.db import dispose

    cfg = Config()
    cfg.RATE_LIMIT_ENABLED = False
    cfg.DATABASE_URL = f"sqlite:///{tmp_path / 'contract.db'}"
    cfg.SECRET_KEY = "k"
    cfg.JWT_MASTER_SECRET = "m"
    dispose()
    create_app(cfg)
    yield
    dispose()


@pytest.mark.parametrize("name", sorted(StrategyRegistry.all()))
def test_strategy_contract(db_env, name):
    # The LLM breakout detector requires a client. Inject a stub that returns
    # no signals so the rest of the contract still runs without a real API key.
    if name == "llm_breakout":
        from app.strategy.llm_breakout import StubClient
        strategy = StrategyRegistry.get(name)(client=StubClient(responses=[{"signals": []}]))
    else:
        strategy = StrategyRegistry.get(name)()
    assert strategy.name == name

    leads = strategy.generate(
        instrument=_fake_instrument(),
        candles=_synthetic_candles(),
        now=None,
    )
    assert isinstance(leads, list)
    for lead in leads:
        assert isinstance(lead, LeadCandidate)
        assert lead.signal_level > 0
        assert lead.direction in ("CALL", "PUT")
        assert lead.underlying_key
        assert lead.signal_type


def test_unknown_strategy_raises():
    with pytest.raises(KeyError):
        StrategyRegistry.get("does_not_exist")


def test_registered_strategies_include_llm_breakout():
    assert "llm_breakout" in StrategyRegistry.all()