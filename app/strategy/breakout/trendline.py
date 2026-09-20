"""Trendline breakout.

Directional break: a close *beyond* the fitted line (lower bound for ascending
support, upper bound for descending resistance) fires a signal; touching the
line is a setup, not a breakdown.
"""

import numpy as np

from app.strategy.breakout.signals import PatternSignal
from app.strategy.breakout.swing import find_swing_highs, find_swing_lows
from app.strategy.scoring import ComponentScores


def _fit(points: list[tuple[int, float]]) -> tuple[float, float, float] | None:
    if len(points) < 2:
        return None
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    slope, intercept = np.polyfit(xs, ys, 1)
    yhat = slope * xs + intercept
    ss_res = float(np.sum((ys - yhat) ** 2))
    ss_tot = float(np.sum((ys - ys.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, intercept, r2


def detect_trendline(df, k: int = 3, min_points: int = 3, proximity_pct: float = 0.5) -> list[PatternSignal]:
    """Directional trendline break; `proximity_pct` is the break tolerance."""
    highs = find_swing_highs(df, k)
    lows = find_swing_lows(df, k)
    n = len(df)
    close = float(df["close"].iloc[-1])
    tolerance = max(0.0, float(proximity_pct)) / 100.0
    signals = []

    if len(lows) >= min_points:
        res = _fit([(i, float(df["low"].iloc[i])) for i in lows[-min_points:]])
        if res and res[0] > 0:  # ascending support
            line_now = res[0] * (n - 1) + res[1]
            if line_now > 0 and close < line_now * (1.0 - tolerance):
                r2 = max(0.0, min(1.0, float(res[2])))
                components = ComponentScores(
                    pattern_fit=r2,
                    volume=0.5,
                    trend_alignment=0.5,
                    proximity=1.0,
                    structure=r2,
                )
                signals.append(PatternSignal(
                    "PUT", "trendline", round(line_now, 2),
                    round(min(0.9, 0.55 + 0.15 * r2 + 0.03 * min_points), 2), components,
                ))

    if len(highs) >= min_points:
        res = _fit([(i, float(df["high"].iloc[i])) for i in highs[-min_points:]])
        if res and res[0] < 0:  # descending resistance
            line_now = res[0] * (n - 1) + res[1]
            if line_now > 0 and close > line_now * (1.0 + tolerance):
                r2 = max(0.0, min(1.0, float(res[2])))
                components = ComponentScores(
                    pattern_fit=r2,
                    volume=0.5,
                    trend_alignment=0.5,
                    proximity=1.0,
                    structure=r2,
                )
                signals.append(PatternSignal(
                    "CALL", "trendline", round(line_now, 2),
                    round(min(0.9, 0.55 + 0.15 * r2 + 0.03 * min_points), 2), components,
                ))

    return signals
