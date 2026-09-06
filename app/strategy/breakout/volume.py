"""Volume spike analysis (Durgia 2025): volume confirmation for breakouts.

A volume spike (>= `multiplier` x the rolling average volume, within the last
`lookback` bars) that coincides with a price break of recent highs/lows is a
higher-quality breakout signal. Also usable as a confirmation filter/boost for
the price-based patterns.
"""

import pandas as pd

from app.strategy.breakout.signals import PatternSignal


def volume_spike(df, multiplier: float = 4.0, window: int = 20, lookback: int = 5) -> bool:
    """True if any of the last `lookback` bars had volume >= multiplier x the
    `window`-bar rolling average volume."""
    if "volume" not in df.columns or len(df) < window + 2:
        return False
    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    avg = vol.rolling(window).mean()
    recent_vol = vol.iloc[-lookback:]
    recent_avg = avg.iloc[-lookback:]
    if recent_avg.isna().any() or (recent_avg <= 0).any():
        return False
    return bool((recent_vol >= multiplier * recent_avg).any())


def detect_volume(df, multiplier: float = 4.0, window: int = 20, lookback: int = 5,
                  proximity_pct: float = 0.5, recent_highs: int = 20) -> list[PatternSignal]:
    """Volume spike + price break of recent highs (CALL) / lows (PUT)."""
    if not volume_spike(df, multiplier, window, lookback):
        return []

    hist = df.iloc[-recent_highs:-1]
    upper = float(hist["high"].max())
    lower = float(hist["low"].min())
    close = float(df["close"].iloc[-1])

    signals = []
    if upper > 0 and close >= upper * (1 - proximity_pct / 100.0):
        signals.append(PatternSignal("CALL", "volume_breakout", round(upper, 2), 0.8))
    if lower > 0 and close <= lower * (1 + proximity_pct / 100.0):
        signals.append(PatternSignal("PUT", "volume_breakout", round(lower, 2), 0.8))
    return signals