"""Breakout strategy (registered as "breakout").

Runs the swing-based pattern detectors (horizontal range, trendline, triangle,
flag/pennant, head & shoulders) over the instrument's daily candles and emits at
most one lead per instrument (the highest-confidence signal, above the
configured minimum confidence).
"""

import logging

import pandas as pd

from app.settings import get_setting
from app.strategy.base import LeadCandidate, Strategy, register_strategy
from app.strategy.breakout.detector import DEFAULT_PATTERNS, best_signal, run_detectors
from app.strategy.breakout.volume import volume_spike

log = logging.getLogger(__name__)


@register_strategy("breakout")
class BreakoutStrategy(Strategy):
    name = "breakout"
    required_interval = "day"

    def generate(self, instrument, candles: pd.DataFrame, now) -> list[LeadCandidate]:
        if candles is None or candles.empty or len(candles) < 40:
            return []

        patterns_enabled = get_setting("breakout.patterns_enabled", DEFAULT_PATTERNS)
        min_confidence = float(get_setting("breakout.min_confidence", 0.6))
        require_spike = bool(get_setting("breakout.require_volume_spike", False))
        volume_boost = float(get_setting("breakout.volume_boost", 0.15))
        cfg = {
            "lookback_days": int(get_setting("breakout.lookback_days", 60)),
            "swing_k": int(get_setting("breakout.swing_k", 3)),
            "proximity_pct": float(get_setting("breakout.proximity_pct", 0.5)),
            "min_touches": int(get_setting("breakout.min_touches", 1)),
            "min_trendline_points": int(get_setting("breakout.min_trendline_points", 3)),
            "pole_pct": float(get_setting("breakout.pole_pct", 3.0)),
            "volume_multiplier": float(get_setting("breakout.volume_multiplier", 4.0)),
            "volume_window": int(get_setting("breakout.volume_window", 20)),
            "volume_lookback": int(get_setting("breakout.volume_lookback", 5)),
        }

        # Volume confirmation (Durgia 2025): spike within the last few bars.
        spike = volume_spike(
            candles,
            multiplier=cfg["volume_multiplier"],
            window=cfg["volume_window"],
            lookback=cfg["volume_lookback"],
        )
        if require_spike and not spike:
            return []

        signals = run_detectors(candles, patterns_enabled, cfg)
        if spike and volume_boost > 0:
            for s in signals:
                s.confidence = round(min(0.95, s.confidence + volume_boost), 2)

        best = best_signal(signals, min_confidence)
        if best is None:
            return []

        log.info("breakout: %s %s @ %.2f (conf %.2f, vol-spike=%s) for %s",
                 best.direction, best.signal_type, best.signal_level, best.confidence, spike, instrument.symbol)
        return [
            LeadCandidate(
                instrument_id=instrument.id,
                underlying_key=instrument.spot_instrument_key,
                direction=best.direction,
                signal_type=best.signal_type,
                signal_level=best.signal_level,
                confidence=best.confidence,
                chart_interval=self.required_interval,
            )
        ]