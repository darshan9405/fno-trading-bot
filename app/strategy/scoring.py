"""Composite scoring for breakout strategy leads.

The previous scoring model assigned each detector its own hand-tuned heuristic
(`0.6 + 0.05*touches`, `0.55 + 0.15*R^2 + 0.03*n_points`, constant `0.7`/`0.8`,
...) with heterogenous ceilings (0.95 / 0.9 / 0.9 / 0.7 / 0.9 / 0.8). They were
not comparable across detectors and none incorporated market context.

This module defines a single normalised scoring pipeline:

    1. Each detector reports its raw `PatternSignal` plus a `ComponentScores`
       dict (pattern_fit, volume, trend_alignment, proximity, structure, ...)
       that captures the evidence it actually observed.
    2. `composite(components)` blends the evidence via fixed weights into a
       single score in [0, 1] with a soft penalty when core components disagree.
    3. Optional Tier-3 add-ons (IV, OI, time-of-day) and Tier-4 calibration/
       staleness-decy are layered on top in the strategy entry point or order
       placer.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping


# --- Default weights -------------------------------------------------------
# Sum to 1.0 (with tier-3 add-ons documented separately in __init__.py).
DEFAULT_WEIGHTS: dict[str, float] = {
    "pattern_fit":     0.40,
    "volume":          0.25,
    "trend_alignment": 0.15,
    "proximity":       0.10,
    "structure":       0.10,
}

# Tier-3 supplementary weights. These get folded into the active weight map when
# their respective context feature is enabled.
DEFAULT_TIER3_WEIGHTS: dict[str, float] = {
    "iv":            0.05,
    "oi":            0.05,
    "time_of_day":   0.05,
}


@dataclass
class ComponentScores:
    """Normalised evidence vector in [0, 1] for every scoring dimension."""

    pattern_fit: float = 0.0      # detector-internal fit quality
    volume: float = 0.5          # 0.5 = neutral (no spike, no penalty)
    trend_alignment: float = 0.5 # 1.0 with trend, 0.0 against, 0.5 unknown
    proximity: float = 0.0       # how close the close is to the trigger (0 chase .. 1 at level)
    structure: float = 0.0       # R^2 / symmetry / touches / span proxy

    extras: dict[str, float] = field(default_factory=dict)

    def with_update(self, **kwargs) -> "ComponentScores":
        return replace(self, **kwargs)

    def with_extra(self, **kwargs) -> "ComponentScores":
        merged = {**self.extras, **{k: float(v) for k, v in kwargs.items()}}
        return ComponentScores(
            pattern_fit=self.pattern_fit,
            volume=self.volume,
            trend_alignment=self.trend_alignment,
            proximity=self.proximity,
            structure=self.structure,
            extras=merged,
        )

    def value(self, name: str) -> float:
        if name in DEFAULT_WEIGHTS:
            return float(getattr(self, name))
        return float(self.extras.get(name, 0.0))


# --- Composite --------------------------------------------------------------


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def available_weight_map(enable_iv: bool = False, enable_oi: bool = False,
                         enable_tod: bool = False) -> dict[str, float]:
    """Return the active weight map for the current configuration.

    Tier-3 features shrink the base weights proportionally so the total stays
    at 1.0. Disabling all tier-3 features collapses to the pure tier-1 weights.
    """
    extras: dict[str, float] = {}
    if enable_iv:
        extras["iv"] = DEFAULT_TIER3_WEIGHTS["iv"]
    if enable_oi:
        extras["oi"] = DEFAULT_TIER3_WEIGHTS["oi"]
    if enable_tod:
        extras["time_of_day"] = DEFAULT_TIER3_WEIGHTS["time_of_day"]

    extra_total = sum(extras.values())
    if extra_total > 0:
        scale = (1.0 - extra_total) / sum(DEFAULT_WEIGHTS.values())
        weights = {k: v * scale for k, v in DEFAULT_WEIGHTS.items()}
    else:
        weights = dict(DEFAULT_WEIGHTS)
    weights.update(extras)
    return weights


def composite(components: ComponentScores,
              weights: Mapping[str, float] | None = None) -> float:
    """Blend component scores into a single number in [0, 1].

    Soft-penalty: if either `pattern_fit`, `volume`, or `trend_alignment` falls
    below `LOW_COMPONENT_THRESHOLD` (a breakdown in core evidence), shrink the
    final score by 15%.
    """
    weights = weights or DEFAULT_WEIGHTS
    score = 0.0
    total_weight = 0.0
    for name, weight in weights.items():
        value = _clamp01(components.value(name))
        score += weight * value
        total_weight += weight

    if total_weight <= 0:
        return 0.0
    score = score / total_weight

    LOW_COMPONENT_THRESHOLD = 0.40
    floor = min(components.pattern_fit, components.volume, components.trend_alignment)
    if floor < LOW_COMPONENT_THRESHOLD:
        score *= 0.85

    return round(_clamp01(score), 4)


# --- Compatibility helpers -------------------------------------------------
# Some non-breakout strategies (or older detectors) still emit a bare
# `confidence` float. Translate it into an equivalent ComponentScores with
# everything else neutral, so they continue to participate in the same pipeline.


def components_from_legacy(legacy_confidence: float) -> ComponentScores:
    return ComponentScores(
        pattern_fit=_clamp01(legacy_confidence),
        volume=0.5,
        trend_alignment=0.5,
        proximity=0.0,
        structure=0.0,
    )
