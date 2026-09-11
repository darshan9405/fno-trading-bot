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
    """Backward-compatible single-pick: the top-1 above `min_confidence`."""
    ranked = rank_signals(signals, top_k=1, min_score=min_confidence)
    return ranked[0] if ranked else None


def rank_signals(signals: list[PatternSignal], top_k: int = 2,
                 min_score: float = 0.6) -> list[PatternSignal]:
    """Return the top-`top_k` signals above `min_score`, preserving detector
    order for ties. Tier-2 of the scoring improvement: an underlying that fires
    multiple patterns (e.g. both CALL and PUT in the same session) now produces
    multiple leads, each carrying its own composite score."""
    if not signals:
        return []
    top_k = max(1, int(top_k))
    min_score = float(min_score)
    eligible = [s for s in signals if float(s.confidence) >= min_score]
    eligible.sort(key=lambda s: float(s.confidence), reverse=True)
    return eligible[:top_k]
