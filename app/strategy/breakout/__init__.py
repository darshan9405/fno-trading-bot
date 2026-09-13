"""Breakout strategy (registered as "breakout").

Runs the swing-based pattern detectors (horizontal range, trendline, triangle,
flag/pennant, head & shoulders) over the instrument's daily candles and emits
leads scored via the composite pipeline in `app.strategy.scoring`.

Top-N emission (Tier-2): `rank_signals` returns the top-`k` signals above the
score floor so a strong underlying that fires both CALL and PUT patterns is
persisted with its full evidence, instead of silently dropping the second one.
"""

import logging

import pandas as pd

from app.settings import get_setting
from app.strategy.base import LeadCandidate, Strategy, register_strategy
from app.strategy.breakout.detector import DEFAULT_PATTERNS, rank_signals, run_detectors
from app.strategy.breakout.market import is_aligned, trend_on_date
from app.strategy.breakout.volume import volume_spike
from app.strategy.scoring import (
    ComponentScores,
    available_weight_map,
    composite,
)

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
        top_k = max(1, int(get_setting("breakout.top_k_per_instrument", 2)))
        alignment = get_setting("breakout.market_alignment", "off")  # "off" | "nifty_sma20"
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

        # --- Volume confirmation ---------------------------------------------------
        spike = volume_spike(
            candles,
            multiplier=cfg["volume_multiplier"],
            window=cfg["volume_window"],
            lookback=cfg["volume_lookback"],
        )
        if require_spike and not spike:
            return []

        # --- Pattern detection ---------------------------------------------------
        signals = run_detectors(candles, patterns_enabled, cfg)

        # --- Compose components ---------------------------------------------------
        # Trend alignment (NIFTY 20-SMA). The `is_aligned` helper returns True for
        # unknown trends, so unknown -> neutral (0.5), aligned -> 1.0, against -> 0.0.
        trend = None
        if alignment == "nifty_sma20" and now is not None:
            try:
                trend = self._trend_lookup(candles, now)
            except Exception as e:
                log.warning("breakout: trend lookup failed (%s); treating as neutral", e)
                trend = None

        # Tier-3 weight map for the generate-time composite score.
        # - TOD is included when `now` is set (we have a real IST clock value).
        # - IV / OI are deliberately NOT included here. They need the option
        #   chain (strike-specific), which is loaded later in `attach_lead_plans`
        #   once the strike is resolved. They're added then via
        #   `tier3.reblend_with_tier3`, which re-blends the stored score.
        enable_tod = bool(get_setting("scoring.enable_time_of_day", False)) and now is not None
        weights = available_weight_map(
            enable_iv=False, enable_oi=False, enable_tod=enable_tod
        )

        for s in signals:
            comps = s.components
            # Apply market alignment (Tier-1, now wired into production)
            comps = comps.with_update(trend_alignment=self._trend_score(s.direction, trend))
            # Apply volume boost: lift `comps.volume` towards 1.0 when spike present
            if spike and volume_boost > 0:
                comps = comps.with_update(volume=min(1.0, comps.volume + volume_boost))
            # Tier-3 features: hook for future attach_market_context(candles, instrument, now)
            comps = self._attach_tier3_context(comps, instrument, now)
            score = composite(comps, weights)
            # Only override `confidence` if the new score is meaningfully different
            # (avoid zeroing heuristic scores that legacy callers might still rely on)
            if score != s.confidence:
                s.confidence = round(score, 4)

        # --- Tier-2 top-N emission ------------------------------------------------
        ranked = rank_signals(signals, top_k=top_k, min_score=min_confidence)
        if not ranked:
            return []

        candidates = []
        for best in ranked:
            log.info(
                "breakout: %s %s @ %.2f (score %.3f, vol-spike=%s, trend=%s) for %s",
                best.direction, best.signal_type, best.signal_level,
                best.confidence, spike, trend, instrument.symbol,
            )
            candidates.append(LeadCandidate(
                instrument_id=instrument.id,
                underlying_key=instrument.spot_instrument_key,
                direction=best.direction,
                signal_type=best.signal_type,
                signal_level=best.signal_level,
                confidence=best.confidence,
                chart_interval=self.required_interval,
                meta={
                    "components": best.components.__dict__,
                    "trend": trend,
                    "volume_spike": spike,
                },
            ))
        return candidates

    # --- helpers --------------------------------------------------------------

    _trend_cache: dict = {}

    def _trend_lookup(self, candles, now) -> str | None:
        """Trend of the underlying itself (proxy for cross-asset alignment).

        For non-NIFTY instruments the ideal is to compare to the NIFTY index
        trend, but doing so requires fetching the NIFTY candle series — kept
        out of the strategy hot path for now. The underlying's own 20-SMA
        trend is a useful proxy: "CALL signals when the stock is trending
        up" is itself a positive bias, even if it's not the broader market.

        Returns None when `now` is None (e.g. during tests); the trend module
        treats unknown trend as neutral.
        """
        day = pd.Timestamp(now).normalize().date().isoformat() if now is not None else "today"
        if day in self._trend_cache:
            return self._trend_cache[day]
        try:
            trend = trend_on_date(candles, pd.Timestamp(now)) if now is not None else None
        except Exception:
            trend = None
        self._trend_cache[day] = trend
        return trend

    @staticmethod
    def _trend_score(direction: str, trend: str | None) -> float:
        if trend is None:
            return 0.5
        return 1.0 if is_aligned(direction, trend) else 0.0

    @staticmethod
    def _attach_tier3_context(comps: ComponentScores, instrument, now) -> ComponentScores:
        """Tier-3 add-on hook (IV / OI / time-of-day context).

        Disabled by default. Each factor is defensive: if its data source is
        unavailable it returns the ComponentScores unchanged rather than
        raising. Only the cheap dimensions (time-of-day) are applied here;
        IV/OI is layered in `order_placer` after the option chain is fetched.
        """
        try:
            from app.strategy import tier3  # noqa: F401
            return tier3.attach_market_context(comps, instrument, now)
        except ImportError:
            return comps
        except Exception:
            return comps
