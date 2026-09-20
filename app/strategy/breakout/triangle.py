"""Triangle breakout.

Accepts symmetrical, ascending, and descending shapes; rejects only
near-parallel fits. Direction comes from which line was breached.
`proximity_pct` is the directional break tolerance.
"""

from app.strategy.breakout.signals import PatternSignal
from app.strategy.breakout.swing import find_swing_highs, find_swing_lows
from app.strategy.breakout.trendline import _fit
from app.strategy.scoring import ComponentScores


_PARALLEL_SLOPE_EPS = 1e-9


def detect_triangle(df, k: int = 3, min_points: int = 3, proximity_pct: float = 0.5) -> list[PatternSignal]:
    """Directional triangle breakout across the three classical shapes."""
    highs = find_swing_highs(df, k)
    lows = find_swing_lows(df, k)
    if len(highs) < min_points or len(lows) < min_points:
        return []

    upper = _fit([(i, float(df["high"].iloc[i])) for i in highs[-min_points:]])
    lower = _fit([(i, float(df["low"].iloc[i])) for i in lows[-min_points:]])
    if not upper or not lower:
        return []

    su, iu, r2u = upper
    sl, il, r2l = lower
    if abs(su - sl) <= _PARALLEL_SLOPE_EPS:
        return []

    n = len(df)
    up_now = su * (n - 1) + iu
    lo_now = sl * (n - 1) + il
    if up_now <= 0 or lo_now <= 0:
        return []

    close = float(df["close"].iloc[-1])
    tolerance = max(0.0, float(proximity_pct)) / 100.0
    r2 = max(0.0, min(1.0, float(min(r2u, r2l))))
    components = ComponentScores(
        pattern_fit=r2,
        volume=0.5,
        trend_alignment=0.5,
        proximity=1.0,
        structure=r2,
    )
    confidence = round(min(0.9, 0.55 + 0.15 * r2), 2)

    signals = []
    if close > up_now * (1.0 + tolerance):
        signals.append(PatternSignal("CALL", "triangle", round(up_now, 2), confidence, components))
    if close < lo_now * (1.0 - tolerance):
        signals.append(PatternSignal("PUT", "triangle", round(lo_now, 2), confidence, components))
    return signals
