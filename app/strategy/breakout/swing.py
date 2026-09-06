"""Swing point detection (fractal method).

A bar is a swing high/low if its high/low is the extreme within `k` bars on
each side. These anchor the pattern detectors (trendlines, triangles, H&S).
"""


def find_swing_highs(df, k: int = 3) -> list[int]:
    highs = df["high"].values
    out = []
    for i in range(k, len(df) - k):
        window = highs[i - k : i + k + 1]
        if highs[i] == window.max() and highs[i] > highs[i - k] and highs[i] > highs[i + k]:
            out.append(i)
    return out


def find_swing_lows(df, k: int = 3) -> list[int]:
    lows = df["low"].values
    out = []
    for i in range(k, len(df) - k):
        window = lows[i - k : i + k + 1]
        if lows[i] == window.min() and lows[i] < lows[i - k] and lows[i] < lows[i + k]:
            out.append(i)
    return out


def swing_points(df, k: int = 3) -> tuple[list[int], list[int]]:
    return find_swing_highs(df, k), find_swing_lows(df, k)