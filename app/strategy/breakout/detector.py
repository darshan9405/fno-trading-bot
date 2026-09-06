"""Detector orchestration: run enabled patterns over candles -> PatternSignals."""

import logging

from app.strategy.breakout.flag_pennant import detect_flag
from app.strategy.breakout.head_shoulders import detect_head_shoulders
from app.strategy.breakout.horizontal import detect_horizontal
from app.strategy.breakout.signals import PatternSignal
from app.strategy.breakout.trendline import detect_trendline
from app.strategy.breakout.triangle import detect_triangle
from app.strategy.breakout.volume import detect_volume, volume_spike

log = logging.getLogger(__name__)

DEFAULT_PATTERNS = ["horizontal_range", "trendline", "triangle", "flag_pennant", "head_shoulders", "volume_breakout"]


def run_detectors(df, patterns_enabled: list[str], cfg: dict | None = None) -> list[PatternSignal]:
    cfg = cfg or {}
    enabled = set(patterns_enabled or DEFAULT_PATTERNS)
    signals: list[PatternSignal] = []

    if "horizontal_range" in enabled:
        signals += detect_horizontal(
            df,
            lookback=int(cfg.get("lookback_days", 60)),
            proximity_pct=float(cfg.get("proximity_pct", 0.5)),
            min_touches=int(cfg.get("min_touches", 1)),
        )
    if "trendline" in enabled:
        signals += detect_trendline(
            df,
            k=int(cfg.get("swing_k", 3)),
            min_points=int(cfg.get("min_trendline_points", 3)),
            proximity_pct=float(cfg.get("proximity_pct", 0.5)),
        )
    if "triangle" in enabled:
        signals += detect_triangle(
            df,
            k=int(cfg.get("swing_k", 3)),
            min_points=int(cfg.get("min_trendline_points", 4)),
            proximity_pct=float(cfg.get("proximity_pct", 0.5)),
        )
    if "flag_pennant" in enabled:
        signals += detect_flag(df, pole_pct=float(cfg.get("pole_pct", 3.0)))
    if "head_shoulders" in enabled:
        signals += detect_head_shoulders(
            df,
            k=int(cfg.get("swing_k", 3)),
            proximity_pct=float(cfg.get("proximity_pct", 0.5)),
        )
    if "volume_breakout" in enabled:
        signals += detect_volume(
            df,
            multiplier=float(cfg.get("volume_multiplier", 4.0)),
            window=int(cfg.get("volume_window", 20)),
            lookback=int(cfg.get("volume_lookback", 5)),
            proximity_pct=float(cfg.get("proximity_pct", 0.5)),
        )

    return signals


def best_signal(signals: list[PatternSignal], min_confidence: float = 0.6) -> PatternSignal | None:
    if not signals:
        return None
    best = max(signals, key=lambda s: s.confidence)
    if best.confidence < min_confidence:
        return None
    return best