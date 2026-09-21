"""Format OHLCV price action into a compact tabular text block for LLM prompts.

The LLM sees raw numbers, not charts. We emit an aligned text table of the last
`n` daily bars (date, OHLC, volume) plus a small context header (today's close,
today's volume, SMA20, SMA50, ATR14) so the LLM has the same indicators a human
analyst would plot. The format is deliberately plain (no pandas/numpy tables)
because LLMs read aligned fixed-width text more reliably than structured objects.
"""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime

import pandas as pd


def _fmt(value: float) -> str:
    """Format a number with thousands separators and 2 decimals (drop trailing .00)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "    ."
    return f"{value:,.2f}"


def _index_to_date(idx_value) -> str:
    """Render a pandas index value (Timestamp / datetime / date) as YYYY-MM-DD."""
    if isinstance(idx_value, pd.Timestamp):
        return idx_value.strftime("%Y-%m-%d")
    if isinstance(idx_value, datetime):
        return idx_value.strftime("%Y-%m-%d")
    if isinstance(idx_value, _date):
        return idx_value.isoformat()
    return str(idx_value)[:10]


def slice_candles(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Return the last `n` rows of `df`. Defensive: returns the full frame when short."""
    if df is None or df.empty:
        return df
    if len(df) <= n:
        return df
    return df.iloc[-n:]


def format_candles_block(df: pd.DataFrame) -> str:
    """Render an OHLCV DataFrame as a fixed-width tabular text block.

    Columns: DATE  OPEN  HIGH  LOW  CLOSE  VOLUME
    Rows are most-recent-last so the LLM reads left-to-right and top-to-bottom
    (chronological). Volume is rendered as an integer (rounded) since fractional
    shares aren't meaningful at NSE scale.
    """
    if df is None or df.empty:
        return "(no candles)"

    lines = ["DATE       OPEN        HIGH        LOW         CLOSE       VOLUME"]
    for ts, row in df.iterrows():
        lines.append(
            f"{_index_to_date(ts)} "
            f"{_fmt(row['open']):>11} "
            f"{_fmt(row['high']):>11} "
            f"{_fmt(row['low']):>11} "
            f"{_fmt(row['close']):>11} "
            f"{int(round(float(row['volume']))):>12,}"
        )
    return "\n".join(lines)


def _safe_series(df: pd.DataFrame, col: str) -> pd.Series | None:
    if col not in df.columns:
        return None
    s = pd.to_numeric(df[col], errors="coerce")
    return s if not s.dropna().empty else None


def compute_indicators(df: pd.DataFrame) -> dict[str, float | None]:
    """Best-effort SMA20 / SMA50 / ATR14 from the full frame.

    Returns None for any indicator whose computation isn't possible
    (insufficient data, missing column). The caller decides what to do.
    """
    out: dict[str, float | None] = {"sma20": None, "sma50": None, "atr14": None}

    close = _safe_series(df, "close")
    high = _safe_series(df, "high")
    low = _safe_series(df, "low")

    if close is not None and len(close) >= 20:
        out["sma20"] = float(close.iloc[-20:].mean())
    if close is not None and len(close) >= 50:
        out["sma50"] = float(close.iloc[-50:].mean())

    if high is not None and low is not None and close is not None and len(df) >= 15:
        prev_close = close.shift(1)
        tr = pd.concat(
            [
                (high - low),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr14 = tr.rolling(14).mean().iloc[-1]
        if not pd.isna(atr14):
            out["atr14"] = float(atr14)

    return out


def last_bar(df: pd.DataFrame) -> dict:
    """Return the last bar's date / close / volume as plain python values."""
    if df is None or df.empty:
        return {"date": None, "close": None, "volume": None}
    last = df.iloc[-1]
    return {
        "date": _index_to_date(df.index[-1]),
        "close": float(last["close"]),
        "volume": float(last["volume"]) if "volume" in df.columns else None,
    }


def build_user_prompt(
    symbol: str,
    underlying_key: str,
    df: pd.DataFrame,
    lookback: int,
    volume_multiplier: float,
    divergence_pct: float,
    min_confidence: float,
) -> str:
    """Assemble the full USER prompt sent to the LLM for one instrument."""
    sliced = slice_candles(df, lookback)
    block = format_candles_block(sliced)
    bar = last_bar(sliced)
    ind = compute_indicators(sliced)

    def _opt(value, fmt_spec="{:.2f}"):
        return "n/a" if value is None else fmt_spec.format(value)

    return (
        f"INTERVAL: 1d (daily)\n"
        f"SYMBOL: {symbol}\n"
        f"UNDERLYING KEY: {underlying_key}\n"
        f"AS-OF DATE: {bar['date'] or 'n/a'}\n"
        f"LAST CLOSE: {_opt(bar['close'])}\n"
        f"LAST VOLUME: {int(bar['volume']) if bar['volume'] is not None else 'n/a'}\n"
        f"SMA20: {_opt(ind['sma20'])}\n"
        f"SMA50: {_opt(ind['sma50'])}\n"
        f"ATR14: {_opt(ind['atr14'])}\n"
        f"VOLUME SPIKE MULTIPLIER: {volume_multiplier:g}x 20d rolling avg\n"
        f"DIVERGENCE TOLERANCE: {divergence_pct:g}%\n"
        f"MIN CONFIDENCE: {min_confidence:g}\n"
        f"LOOKBACK: {lookback} sessions\n"
        f"\n"
        f"{block}\n"
        f"\n"
        f"Return JSON only.\n"
    )
