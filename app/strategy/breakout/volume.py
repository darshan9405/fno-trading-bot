"""Volume spike analysis (Durgia 2025): volume confirmation for breakouts.

A volume spike (>= `multiplier` x the rolling average volume, within the last
`lookback` bars) that coincides with a price break of recent highs/lows is a
higher-quality breakout signal. Also usable as a confirmation filter/boost for
the price-based patterns.
"""

import pandas as pd

from app.strategy.breakout.signals import PatternSignal
from app.strategy.scoring import ComponentScores


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


def _volume_quality(df, multiplier: float, window: int, lookback: int) -> float:
    """How strongly confirmed is the recent volume spike, in [0, 1]?
    Neutral (0.5) when no spike, ramps to 1.0 for extreme spikes."""
    if "volume" not in df.columns or len(df) < window + 2:
        return 0.5
    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    avg = vol.rolling(window).mean()
    recent_vol = vol.iloc[-lookback:]
    recent_avg = avg.iloc[-lookback:].replace(0, pd.NA).dropna()
    if recent_avg.empty:
        return 0.5
    ratios = (recent_vol.values / recent_avg.values)
    ratios = ratios[~pd.isna(ratios)]
    if ratios.size == 0:
        return 0.5
    max_ratio = float(max(ratios))
    if max_ratio < multiplier:
        return 0.5
    # Map ratio to [0.5, 1.0]: ratio == multiplier -> 0.5, ratio == 2*multiplier -> 1.0
    excess = (max_ratio - multiplier) / max(multiplier, 1e-9)
    return max(0.5, min(1.0, 0.5 + excess / 2.0))


def detect_volume(df, multiplier: float = 4.0, window: int = 20, lookback: int = 5,
                  proximity_pct: float = 0.5, recent_highs: int = 20) -> list[PatternSignal]:
    """Volume spike + price break of recent highs (CALL) / lows (PUT).

    At most one signal per bar; direction is the side actually broken. A close
    sitting inside the band with a volume spike is intentionally not a signal.
    """
    spike = volume_spike(df, multiplier, window, lookback)
    if not spike:
        return []

    vol_quality = _volume_quality(df, multiplier, window, lookback)

    hist = df.iloc[-recent_highs:-1]
    upper = float(hist["high"].max())
    lower = float(hist["low"].min())
    close = float(df["close"].iloc[-1])

    # pattern_fit=0.85 reflects that volume_breakout passes two gates
    # (spike AND price break), making it stronger than price-only patterns.
    components_template = ComponentScores(
        pattern_fit=0.85,
        volume=vol_quality,
        trend_alignment=0.5,
        proximity=1.0,
        structure=vol_quality,
    )

    broke_above = upper > 0 and close > upper * (1 + proximity_pct / 100.0)
    broke_below = lower > 0 and close < lower * (1 - proximity_pct / 100.0)

    if broke_above and not broke_below:
        return [PatternSignal("CALL", "volume_breakout", round(upper, 2), 0.8, components_template)]
    if broke_below and not broke_above:
        return [PatternSignal("PUT", "volume_breakout", round(lower, 2), 0.8, components_template)]
    return []  # close inside band, or both sides broken (ambiguous)
