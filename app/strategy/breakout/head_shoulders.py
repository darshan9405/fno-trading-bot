"""Head & Shoulders / Inverse Head & Shoulders breakout.

Regular H&S: three swing highs with the middle (head) the highest and the two
shoulders roughly equal; the neckline runs through the two troughs between
them. A close below the neckline -> PUT (bearish reversal).

Inverse H&S is the mirror (three swing lows, head lowest, neckline through the
two peaks); a close above the neckline -> CALL.
"""

from app.strategy.breakout.signals import PatternSignal
from app.strategy.breakout.swing import find_swing_highs, find_swing_lows
from app.strategy.scoring import ComponentScores


def _neckline_at_today(p1: tuple[int, float], p2: tuple[int, float], n: int) -> float | None:
    if p2[0] == p1[0]:
        return None
    slope = (p2[1] - p1[1]) / (p2[0] - p1[0])
    return slope * (n - 1) + (p1[1] - slope * p1[0])


def detect_head_shoulders(df, k: int = 3, shoulder_tol: float = 0.10,
                          proximity_pct: float = 0.5) -> list[PatternSignal]:
    highs = find_swing_highs(df, k)
    lows = find_swing_lows(df, k)
    n = len(df)
    close = float(df["close"].iloc[-1])
    signals = []

    # --- Regular H&S (bearish) ---
    if len(highs) >= 3:
        h = highs[-3:]
        hvals = [float(df["high"].iloc[i]) for i in h]
        head_pos = hvals.index(max(hvals))
        if head_pos == 1:  # head is the middle peak
            left, right = hvals[0], hvals[2]
            if left > 0 and right > 0 and abs(left - right) / max(left, right) <= shoulder_tol:
                l_before = min((i for i in lows if h[0] < i < h[1]), key=lambda i: float(df["low"].iloc[i]), default=None)
                l_after = min((i for i in lows if h[1] < i < h[2]), key=lambda i: float(df["low"].iloc[i]), default=None)
                if l_before is not None and l_after is not None:
                    neck = _neckline_at_today(
                        (l_before, float(df["low"].iloc[l_before])),
                        (l_after, float(df["low"].iloc[l_after])),
                        n,
                    )
                    if neck and neck > 0 and (neck * (1 - proximity_pct / 100.0) <= close <= neck * (1 + proximity_pct / 100.0)):
                        symmetry = 1.0 - abs(left - right) / max(left, right)
                        structure = max(0.0, min(1.0, symmetry))
                        components = ComponentScores(
                            pattern_fit=structure,
                            volume=0.5,
                            trend_alignment=0.5,
                            proximity=1.0,
                            structure=structure,
                        )
                        signals.append(PatternSignal(
                            "PUT", "head_shoulders", round(neck, 2),
                            round(min(0.9, 0.6 + 0.2 * structure), 2), components,
                        ))

    # --- Inverse H&S (bullish) ---
    if len(lows) >= 3:
        l = lows[-3:]
        lvals = [float(df["low"].iloc[i]) for i in l]
        head_pos = lvals.index(min(lvals))
        if head_pos == 1:
            left, right = lvals[0], lvals[2]
            if left > 0 and right > 0 and abs(left - right) / max(left, right) <= shoulder_tol:
                p_before = max((i for i in highs if l[0] < i < l[1]), key=lambda i: float(df["high"].iloc[i]), default=None)
                p_after = max((i for i in highs if l[1] < i < l[2]), key=lambda i: float(df["high"].iloc[i]), default=None)
                if p_before is not None and p_after is not None:
                    neck = _neckline_at_today(
                        (p_before, float(df["high"].iloc[p_before])),
                        (p_after, float(df["high"].iloc[p_after])),
                        n,
                    )
                    if neck and neck > 0 and (neck * (1 - proximity_pct / 100.0) <= close <= neck * (1 + proximity_pct / 100.0)):
                        symmetry = 1.0 - abs(left - right) / max(left, right)
                        structure = max(0.0, min(1.0, symmetry))
                        components = ComponentScores(
                            pattern_fit=structure,
                            volume=0.5,
                            trend_alignment=0.5,
                            proximity=1.0,
                            structure=structure,
                        )
                        signals.append(PatternSignal(
                            "CALL", "head_shoulders", round(neck, 2),
                            round(min(0.9, 0.6 + 0.2 * structure), 2), components,
                        ))

    return signals
