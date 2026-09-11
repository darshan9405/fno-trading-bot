"""Horizontal / range breakout.

Signal when the price is *exactly at* the N-day range boundary (not chasing a
runaway move). Above the range high -> CALL; below the range low -> PUT.
Confidence rises with the number of times the boundary was tested.
"""

from app.strategy.breakout.signals import PatternSignal
from app.strategy.scoring import ComponentScores


def detect_horizontal(df, lookback: int = 60, proximity_pct: float = 0.5,
                      min_touches: int = 1, min_range_pct: float = 0.5) -> list[PatternSignal]:
    if len(df) < lookback + 5:
        return []

    hist = df.iloc[-(lookback + 1):-1]  # prior bars, excluding today
    upper = float(hist["high"].max())
    lower = float(hist["low"].min())
    mid = (upper + lower) / 2.0
    if mid <= 0:
        return []

    range_pct = (upper - lower) / mid * 100.0
    if range_pct < min_range_pct:
        return []  # degenerate / too-tight range

    close = float(df["close"].iloc[-1])

    touches_up = int((hist["high"].values >= upper * (1 - proximity_pct / 100.0)).sum())
    touches_dn = int((hist["low"].values <= lower * (1 + proximity_pct / 100.0)).sum())

    signals = []
    # Two-sided band: price is *at* the level (within proximity), not chasing a runaway move.
    if (upper * (1 - proximity_pct / 100.0) <= close <= upper * (1 + proximity_pct / 100.0)
            and touches_up >= min_touches):
        pattern_fit = min(1.0, touches_up / 8.0)
        components = ComponentScores(
            pattern_fit=pattern_fit,
            volume=0.5,
            trend_alignment=0.5,
            proximity=1.0,
            structure=pattern_fit,
        )
        signals.append(PatternSignal(
            "CALL", "horizontal_range", round(upper, 2), round(0.6 + 0.05 * touches_up, 2), components
        ))
    if (lower * (1 - proximity_pct / 100.0) <= close <= lower * (1 + proximity_pct / 100.0)
            and touches_dn >= min_touches):
        pattern_fit = min(1.0, touches_dn / 8.0)
        components = ComponentScores(
            pattern_fit=pattern_fit,
            volume=0.5,
            trend_alignment=0.5,
            proximity=1.0,
            structure=pattern_fit,
        )
        signals.append(PatternSignal(
            "PUT", "horizontal_range", round(lower, 2), round(0.6 + 0.05 * touches_dn, 2), components
        ))
    return signals
