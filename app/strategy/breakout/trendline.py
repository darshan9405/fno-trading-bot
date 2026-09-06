"""Trendline breakout.

Fit a regression line through the last M swing lows (ascending support) or the
last M swing highs (descending resistance). Signal when the price is at the
line — a break below ascending support -> PUT; a break above descending
resistance -> CALL. Confidence reflects fit quality (R^2).
"""

import numpy as np

from app.strategy.breakout.signals import PatternSignal
from app.strategy.breakout.swing import find_swing_highs, find_swing_lows


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
    highs = find_swing_highs(df, k)
    lows = find_swing_lows(df, k)
    n = len(df)
    close = float(df["close"].iloc[-1])
    signals = []

    if len(lows) >= min_points:
        res = _fit([(i, float(df["low"].iloc[i])) for i in lows[-min_points:]])
        if res and res[0] > 0:  # ascending support
            line_now = res[0] * (n - 1) + res[1]
            if line_now > 0 and abs(close - line_now) / line_now * 100.0 <= proximity_pct:
                conf = min(0.9, 0.55 + 0.15 * res[2] + 0.03 * min_points)
                signals.append(PatternSignal("PUT", "trendline", round(line_now, 2), round(conf, 2)))

    if len(highs) >= min_points:
        res = _fit([(i, float(df["high"].iloc[i])) for i in highs[-min_points:]])
        if res and res[0] < 0:  # descending resistance
            line_now = res[0] * (n - 1) + res[1]
            if line_now > 0 and abs(close - line_now) / line_now * 100.0 <= proximity_pct:
                conf = min(0.9, 0.55 + 0.15 * res[2] + 0.03 * min_points)
                signals.append(PatternSignal("CALL", "trendline", round(line_now, 2), round(conf, 2)))

    return signals