"""Triangle breakout.

Best-fit lines through the last M swing highs (upper) and swing lows (lower)
must converge. When the price is at the converging envelope, a break above the
upper line -> CALL; below the lower line -> PUT.
"""

from app.strategy.breakout.signals import PatternSignal
from app.strategy.breakout.swing import find_swing_highs, find_swing_lows
from app.strategy.breakout.trendline import _fit
from app.strategy.scoring import ComponentScores


def detect_triangle(df, k: int = 3, min_points: int = 3, proximity_pct: float = 0.5) -> list[PatternSignal]:
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
    if su >= sl:  # diverging or parallel — not a triangle
        return []

    n = len(df)
    up_now = su * (n - 1) + iu
    lo_now = sl * (n - 1) + il
    close = float(df["close"].iloc[-1])
    r2 = max(0.0, min(1.0, float(min(r2u, r2l))))
    pattern_fit = r2
    components = ComponentScores(
        pattern_fit=pattern_fit,
        volume=0.5,
        trend_alignment=0.5,
        proximity=1.0,
        structure=pattern_fit,
    )
    conf_lambda = lambda: round(min(0.9, 0.55 + 0.15 * r2), 2)  # noqa: E731

    signals = []
    if up_now > 0 and abs(close - up_now) / up_now * 100.0 <= proximity_pct:
        signals.append(PatternSignal("CALL", "triangle", round(up_now, 2), conf_lambda(), components))
    if lo_now > 0 and abs(close - lo_now) / lo_now * 100.0 <= proximity_pct:
        signals.append(PatternSignal("PUT", "triangle", round(lo_now, 2), conf_lambda(), components))
    return signals
