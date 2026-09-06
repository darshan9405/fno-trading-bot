"""Flag / pennant breakout.

A strong directional "pole" move, followed by a tight consolidation. A break
out of the consolidation in the pole direction is a continuation signal:
pole up + break above consolidation high -> CALL; pole down + break below
consolidation low -> PUT.
"""

from app.strategy.breakout.signals import PatternSignal


def detect_flag(df, pole_len: int = 8, consolidation_len: int = 6,
                pole_pct: float = 3.0, shrink: float = 0.6) -> list[PatternSignal]:
    n = len(df)
    if n < pole_len + consolidation_len + 1:  # pole + consolidation + today
        return []

    pole_start = n - pole_len - consolidation_len
    pole_end = n - consolidation_len
    pole = df.iloc[pole_start:pole_end]
    cons = df.iloc[pole_end:-1]  # consolidation bars strictly before today
    if len(pole) < 2 or len(cons) < 2:
        return []

    start_price = float(pole["close"].iloc[0])
    end_price = float(pole["close"].iloc[-1])
    if start_price <= 0:
        return []

    pole_move = (end_price - start_price) / start_price * 100.0
    if abs(pole_move) < pole_pct:
        return []

    cons_range = float(cons["high"].max() - cons["low"].min())
    pole_span = abs(end_price - start_price)
    if pole_span <= 0 or cons_range > pole_span * shrink:
        return []  # not a tight consolidation

    close = float(df["close"].iloc[-1])
    up = float(cons["high"].max())
    lo = float(cons["low"].min())

    signals = []
    if pole_move > 0 and close >= up:
        signals.append(PatternSignal("CALL", "flag_pennant", round(up, 2), 0.7))
    elif pole_move < 0 and close <= lo:
        signals.append(PatternSignal("PUT", "flag_pennant", round(lo, 2), 0.7))
    return signals