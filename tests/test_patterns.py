"""Breakout pattern engine tests (synthetic OHLC series)."""

import numpy as np
import pandas as pd
import pytest

from app.strategy.breakout.detector import best_signal, run_detectors
from app.strategy.breakout.head_shoulders import detect_head_shoulders
from app.strategy.breakout.horizontal import detect_horizontal
from app.strategy.breakout.flag_pennant import detect_flag
from app.strategy.breakout.swing import find_swing_highs, find_swing_lows
from app.strategy.breakout.volume import detect_volume, volume_spike
from app.strategy import StrategyRegistry


@pytest.fixture
def db_env(tmp_path):
    """Real DB so the strategy can read `settings` (breakout.*)."""
    from app import create_app
    from app.config import Config
    from app.db import dispose

    cfg = Config()
    cfg.RATE_LIMIT_ENABLED = False
    cfg.DATABASE_URL = f"sqlite:///{tmp_path / 'p.db'}"
    cfg.SECRET_KEY = "k"
    cfg.JWT_MASTER_SECRET = "m"
    dispose()
    create_app(cfg)
    yield
    dispose()


def _df(closes, highs=None, lows=None, opens=None):
    n = len(closes)
    highs = highs or [c + 1 for c in closes]
    lows = lows or [c - 1 for c in closes]
    opens = opens or [c for c in closes]
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": 1000.0, "oi": 10000.0},
        index=idx,
    )


def _interp(points: list[tuple[int, float]]) -> list[float]:
    """Linearly interpolate bar values through (index, value) anchor points."""
    points = sorted(points)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    out = []
    for i in range(points[-1][0] + 1):
        j = 0
        while j < len(xs) - 1 and xs[j + 1] < i:
            j += 1
        x0, x1 = xs[j], xs[j + 1]
        y0, y1 = ys[j], ys[j + 1]
        out.append(round(y0 + (y1 - y0) * (i - x0) / (x1 - x0), 2))
    return out


# --- swing detection -----------------------------------------------------


def test_swing_detection():
    closes = _interp([(0, 100), (6, 120), (12, 90), (18, 110), (24, 95)])
    df = _df(closes)
    highs = find_swing_highs(df, k=3)
    lows = find_swing_lows(df, k=3)
    assert 6 in highs and 18 in highs
    assert 12 in lows  # 24 is within the final k bars and cannot be confirmed


# --- horizontal range ----------------------------------------------------


def test_horizontal_range_breakout_call():
    closes = [105.0] * 65 + [110.2]
    highs = [110.0] * 65 + [111.0]
    lows = [100.0] * 65 + [105.0]
    df = _df(closes, highs, lows)
    signals = run_detectors(df, ["horizontal_range"])
    assert signals
    assert signals[0].direction == "CALL"
    assert signals[0].signal_level == 110.0
    assert signals[0].confidence >= 0.6


def test_horizontal_range_no_chase():
    # close way above the range -> no signal (avoids chasing)
    closes = [105.0] * 65 + [112.0]
    highs = [110.0] * 65 + [112.5]
    lows = [100.0] * 65 + [108.0]
    df = _df(closes, highs, lows)
    assert run_detectors(df, ["horizontal_range"]) == []


# --- head & shoulders ----------------------------------------------------


def test_head_shoulders_put():
    closes = _interp(
        [
            (0, 100), (6, 110), (11, 100), (16, 125), (21, 105),
            (26, 118), (31, 114), (35, 111.5),
        ]
    )
    df = _df(closes)
    signals = detect_head_shoulders(df)
    assert signals
    assert signals[0].direction == "PUT"
    assert signals[0].signal_type == "head_shoulders"


def test_inverse_head_shoulders_call():
    # mirror: 3 troughs, middle lowest, close near/above the neckline
    closes = _interp(
        [
            (0, 150), (6, 140), (11, 150), (16, 125), (21, 145),
            (26, 132), (31, 136), (35, 139.5),
        ]
    )
    df = _df(closes)
    signals = detect_head_shoulders(df)
    assert signals
    assert signals[0].direction == "CALL"
    assert signals[0].signal_type == "head_shoulders"


# --- flag / pennant ------------------------------------------------------


def test_flag_continuation_call():
    closes = list(np.linspace(100.0, 115.0, 8)) + [114.0] * 6 + [115.5]
    highs = [c + 0.5 for c in closes[:14]] + [116.0]
    lows = [c - 0.5 for c in closes[:14]] + [114.0]
    highs[8:14] = [115.0] * 6
    lows[8:14] = [113.0] * 6
    df = _df(closes, highs, lows)
    signals = detect_flag(df)
    assert signals
    assert signals[0].direction == "CALL"
    assert signals[0].signal_type == "flag_pennant"


# --- volume confirmation (Durgia 2025) -----------------------------------


def test_volume_spike_detects_anomalous_volume():
    closes = [100.0] * 40
    volume = [1000.0] * 39 + [5000.0]  # 5x the 20d avg on the last bar
    df = _df(closes)
    df["volume"] = volume
    assert volume_spike(df, multiplier=4.0, window=20, lookback=5) is True


def test_volume_spike_no_false_positive():
    closes = [100.0] * 40
    volume = [1000.0] * 40
    df = _df(closes)
    df["volume"] = volume
    assert volume_spike(df, multiplier=4.0, window=20, lookback=5) is False


def test_volume_breakout_detector_call():
    closes = [100.0] * 25 + [105.0]
    highs = [101.0] * 25 + [105.5]
    lows = [99.0] * 25 + [104.0]
    df = _df(closes, highs, lows)
    df["volume"] = [1000.0] * 24 + [5000.0, 5000.0]  # spike in the last 2 bars

    signals = detect_volume(df)
    assert signals
    assert signals[0].direction == "CALL"
    assert signals[0].signal_type == "volume_breakout"
    assert signals[0].confidence == 0.8


def test_volume_breakout_requires_spike():
    closes = [100.0] * 25 + [105.0]
    highs = [101.0] * 25 + [105.5]
    lows = [99.0] * 25 + [104.0]
    df = _df(closes, highs, lows)
    df["volume"] = [1000.0] * 26
    assert detect_volume(df) == []


def test_breakout_strategy_volume_filter(db_env):
    from types import SimpleNamespace

    from app.settings import set_setting

    set_setting("breakout.require_volume_spike", True)
    set_setting("breakout.volume_boost", 0.0)

    strategy = StrategyRegistry.get("breakout")()
    instrument = SimpleNamespace(id=1, symbol="NIFTY", spot_instrument_key="NSE_INDEX|Nifty 50")

    # range breakout, but NO volume spike -> no lead
    df = _df([105.0] * 65 + [110.2], [110.0] * 65 + [111.0], [100.0] * 65 + [105.0])
    df["volume"] = [1000.0] * 66
    assert strategy.generate(instrument, df, now=None) == []

    # same breakout WITH a volume spike -> lead emitted
    df2 = _df([105.0] * 65 + [110.2], [110.0] * 65 + [111.0], [100.0] * 65 + [105.0])
    df2["volume"] = [1000.0] * 65 + [6000.0]
    leads = strategy.generate(instrument, df2, now=None)
    assert len(leads) == 1
    assert leads[0].direction == "CALL"


def test_breakout_strategy_volume_boost(db_env):
    from types import SimpleNamespace

    from app.settings import set_setting

    set_setting("breakout.require_volume_spike", False)
    set_setting("breakout.volume_boost", 0.3)
    set_setting("breakout.min_confidence", 0.9)

    strategy = StrategyRegistry.get("breakout")()
    instrument = SimpleNamespace(id=1, symbol="NIFTY", spot_instrument_key="NSE_INDEX|Nifty 50")

    # horizontal range signal conf ~0.95 -> base already high; use a lower-base pattern instead:
    # volume spike present should push confidence up (0.8 -> 0.9+ when combined with volume_breakout)
    closes = [100.0] * 39 + [105.0]
    highs = [101.0] * 39 + [105.5]
    lows = [99.0] * 39 + [104.0]
    df = _df(closes, highs, lows)
    df["volume"] = [1000.0] * 38 + [5000.0, 5000.0]
    leads = strategy.generate(instrument, df, now=None)
    assert len(leads) == 1  # volume_breakout (0.8 + 0.3 = 1.0 -> capped 0.95) passes min_confidence 0.9


# --- detector / strategy wiring ------------------------------------------


def test_best_signal_filters_by_confidence():
    from app.strategy.breakout.signals import PatternSignal

    signals = [PatternSignal("CALL", "x", 100.0, 0.5), PatternSignal("PUT", "y", 99.0, 0.9)]
    assert best_signal(signals, min_confidence=0.8).direction == "PUT"
    assert best_signal(signals, min_confidence=0.95) is None


def test_breakout_strategy_emits_lead(db_env):
    from app.settings import set_setting

    set_setting("breakout.require_volume_spike", False)
    set_setting("breakout.patterns_enabled", ["horizontal_range"])  # isolate the price-based path
    strategy = StrategyRegistry.get("breakout")()
    closes = [105.0] * 65 + [110.2]
    highs = [110.0] * 65 + [111.0]
    lows = [100.0] * 65 + [105.0]
    df = _df(closes, highs, lows)

    from types import SimpleNamespace

    instrument = SimpleNamespace(id=1, symbol="NIFTY", spot_instrument_key="NSE_INDEX|Nifty 50")
    leads = strategy.generate(instrument, df, now=None)
    assert len(leads) == 1
    assert leads[0].direction == "CALL"
    assert leads[0].underlying_key == "NSE_INDEX|Nifty 50"
    assert leads[0].signal_level == 110.0


def test_breakout_strategy_flat_market_no_lead(db_env):
    strategy = StrategyRegistry.get("breakout")()
    closes = [105.0] * 80
    df = _df(closes)
    from types import SimpleNamespace

    instrument = SimpleNamespace(id=1, symbol="NIFTY", spot_instrument_key="NSE_INDEX|Nifty 50")
    assert strategy.generate(instrument, df, now=None) == []