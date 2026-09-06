"""Market-alignment filter: only trade WITH the NIFTY trend, never against it.

A breakout in the direction of the broader market has a higher probability of
continuing; counter-trend breakouts are more often false. Uses NIFTY's close vs
its 20-day SMA as the trend signal.
"""


def trend_on_date(df, on_date, sma: int = 20):
    """Trend ('up' | 'down' | None) of the index as of `on_date` (inclusive)."""
    closes = df["close"].astype(float)
    sub = closes[closes.index <= on_date]
    if len(sub) < sma + 1:
        return None
    last = float(sub.iloc[-1])
    mean = float(sub.iloc[-sma:].mean())
    return "up" if last > mean else "down"


def is_aligned(direction: str, trend: str | None) -> bool:
    if trend is None:
        return True  # unknown trend -> don't block
    return (direction == "CALL" and trend == "up") or (direction == "PUT" and trend == "down")