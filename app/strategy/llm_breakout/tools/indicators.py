"""`compute_indicators` tool — breakout math the LLM would otherwise fluff.

The LLM is great at pattern recognition but unreliable at precision math
on a candle series. This tool takes the (already-sliced) candles and runs
the deterministic indicator computations the breakout verdict depends on:
  - ATR (Average True Range over N bars) — volatility floor for SL distance
  - 20/50-day EMA — trend direction
  - 20-day rolling high/low — horizontal range break levels
  - ADX (Average Directional Index, Wilder) — trend strength 0-100
  - Bollinger band z-score — squeeze / expansion
  - RSI(14) — overbought/oversold context
  - 52-week high/low and % distance from each
  - Volume z-score vs 20-bar rolling mean — confirmation filter
  - Swing-point fractal detection (N-bar extrema) — support/resistance levels
  - Pivot points (Classic) — daily pivot, R1/S1, R2/S2

Returns a flat JSON dict the LLM can read in its tool-result message. Pure
function — no broker / network calls.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from app.strategy.llm_breakout.tools.base import schema


def _ema(series: pd.Series, span: int) -> float | None:
    if len(series) < span:
        return None
    return float(series.ewm(span=span, adjust=False).mean().iloc[-1])


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def _atr(df: pd.DataFrame, period: int) -> float | None:
    if len(df) < period + 1:
        return None
    tr = _true_range(df).iloc[1:]
    return float(tr.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])


def _rsi(series: pd.Series, period: int = 14) -> float | None:
    if len(series) < period + 1:
        return None
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / period, adjust=False).mean()
    if loss.iloc[-1] == 0:
        return 100.0 if gain.iloc[-1] > 0 else 50.0
    rs = gain.iloc[-1] / loss.iloc[-1]
    return float(100.0 - (100.0 / (1.0 + rs)))


def _adx(df: pd.DataFrame, period: int = 14) -> float | None:
    """Wilder ADX over `period` bars. Returns 0-100."""
    if len(df) < period * 2 + 1:
        return None
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    plus_dm = (high.diff()).where((high.diff() > low.diff().abs()) & (high.diff() > 0), 0.0).astype(float)
    minus_dm = (-low.diff()).where((-low.diff() > high.diff()) & (-low.diff() > 0), 0.0).astype(float)
    tr = _true_range(df)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    plus_di = 100.0 * (plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr)
    minus_di = 100.0 * (minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr)
    # DX = 100 * |+DI - -DI| / (+DI + -DI). Guard against zero denominator by
    # treating both DI == 0 as DX = 0. Force float dtype so the subsequent
    # `.ewm().mean()` doesn't trip the rolling-mean on object dtype.
    denom = plus_di + minus_di
    dx = (100.0 * (plus_di - minus_di).abs() / denom.where(denom > 0)).fillna(0.0).astype(float)
    adx = dx.ewm(alpha=1.0 / period, adjust=False).mean()
    val = adx.iloc[-1]
    if pd.isna(val):
        return None
    return float(val)


def _bollinger(series: pd.Series, period: int = 20, k: float = 2.0) -> dict[str, float | None]:
    if len(series) < period:
        return {"middle": None, "upper": None, "lower": None, "zscore": None}
    sma = series.rolling(period).mean().iloc[-1]
    sd = series.rolling(period).std(ddof=0).iloc[-1]
    upper = sma + k * sd
    lower = sma - k * sd
    last = series.iloc[-1]
    zscore = (last - sma) / sd if sd > 0 else 0.0
    return {
        "middle": float(sma),
        "upper": float(upper),
        "lower": float(lower),
        "zscore": float(zscore),
    }


def _rolling_extrema(series: pd.Series, window: int) -> tuple[float | None, float | None]:
    if len(series) < window:
        return None, None
    return float(series.iloc[-window:].max()), float(series.iloc[-window:].min())


def _swing_points(series: pd.Series, k: int = 2) -> dict[str, list[dict[str, Any]]]:
    """Fractal swing highs/lows: a bar is a swing high/low iff it is the
    extreme within ±k bars. Returns the bar's index as ISO string and price."""
    if len(series) < 2 * k + 1:
        return {"highs": [], "lows": []}
    highs, lows = [], []
    for i in range(k, len(series) - k):
        window = series.iloc[i - k:i + k + 1]
        val = series.iloc[i]
        # Bar index — timestamps become ISO strings, integers stay ints.
        idx_raw = series.index[i]
        if hasattr(idx_raw, "isoformat"):
            idx_repr = idx_raw.isoformat()
        else:
            idx_repr = str(idx_raw)
        if val == window.max():
            highs.append({"index": idx_repr, "price": float(val)})
        if val == window.min():
            lows.append({"index": idx_repr, "price": float(val)})
    return {"highs": highs[-10:], "lows": lows[-10:]}


def _pivots(df: pd.DataFrame) -> dict[str, float | None]:
    if len(df) < 2:
        return {"pp": None, "r1": None, "s1": None, "r2": None, "s2": None}
    prev = df.iloc[-2]
    h, l, c = float(prev["high"]), float(prev["low"]), float(prev["close"])
    pp = (h + l + c) / 3.0
    r1 = 2 * pp - l
    s1 = 2 * pp - h
    r2 = pp + (h - l)
    s2 = pp - (h - l)
    return {"pp": pp, "r1": r1, "s1": s1, "r2": r2, "s2": s2}


class IndicatorsTool:
    name = "compute_indicators"
    description = (
        "Compute breakout-relevant technical indicators from the per-instrument "
        "candles already provided in the system prompt. Returns ATR, EMA(20/50), "
        "ADX(14), RSI(14), Bollinger(20,2) z-score, 20/50-bar high-low range, "
        "52w high/low + % distance, volume z-score, swing-point highs/lows, and "
        "Classic pivot points. Always use this tool BEFORE emitting your final "
        "verdict — the model is unreliable at precision math on candle series."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def __init__(self, context: dict[str, Any]) -> None:
        self._candles: pd.DataFrame | None = context.get("candles")

    def run(self, args: dict[str, Any]) -> dict[str, Any]:
        df = self._candles
        if df is None or df.empty:
            return {"error": "no candles in context"}
        try:
            close = df["close"]
            high = df["high"]
            low = df["low"]
            vol = df["volume"] if "volume" in df.columns else pd.Series([0] * len(df))
            last = float(close.iloc[-1])

            atr14 = _atr(df, 14)
            ema20 = _ema(close, 20)
            ema50 = _ema(close, 50)
            adx14 = _adx(df, 14)
            rsi14 = _rsi(close, 14)
            bb = _bollinger(close)
            hi20, lo20 = _rolling_extrema(high, 20)
            hi50, lo50 = _rolling_extrema(high, 50)
            hi252, lo252 = _rolling_extrema(high, 252), _rolling_extrema(low, 252)
            if isinstance(hi252, tuple):
                hi252 = hi252[0]
            if isinstance(lo252, tuple):
                lo252 = lo252[1]

            vol_mean = float(vol.tail(20).mean()) if len(vol) >= 20 else None
            vol_std = float(vol.tail(20).std(ddof=0)) if len(vol) >= 20 else None
            vol_last = float(vol.iloc[-1]) if len(vol) else 0.0
            vol_z = (vol_last - vol_mean) / vol_std if (vol_mean and vol_std and vol_std > 0) else 0.0

            swings = _swing_points(high, k=2)
            pivots = _pivots(df)

            pct_from_52w_high = ((last - hi252) / hi252 * 100.0) if hi252 else None
            pct_from_52w_low = ((last - lo252) / lo252 * 100.0) if lo252 else None

            return {
                "last_close": last,
                "atr_14": atr14,
                "ema_20": ema20,
                "ema_50": ema50,
                "adx_14": adx14,
                "rsi_14": rsi14,
                "bollinger_20_2": bb,
                "rolling_20d": {"high": hi20, "low": lo20},
                "rolling_50d": {"high": hi50, "low": lo50},
                "year_high_low": {"high": hi252, "low": lo252,
                                  "pct_from_high": pct_from_52w_high,
                                  "pct_from_low": pct_from_52w_low},
                "volume": {"last": vol_last, "mean_20": vol_mean, "std_20": vol_std, "zscore": vol_z},
                "swing_points": swings,
                "pivots": pivots,
            }
        except Exception as e:  # noqa: BLE001
            return {"error": f"compute_indicators failed: {e}"}

    @staticmethod
    def to_schema() -> dict[str, Any]:
        return schema(IndicatorsTool.name, IndicatorsTool.description, IndicatorsTool.parameters)