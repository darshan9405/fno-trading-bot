"""Post-LLM structural validation.

The LLM is the analyst — it decides WHICH pattern and WHETHER to emit. This
module is the strict structural filter that drops bad output:

  - pattern_type not in the allowed 5 (drops `volume_breakout` if LLM tries)
  - direction not in {CALL, PUT}
  - trigger_price not finite / ≤ 0 / outside divergence band
  - confidence not finite / below min_confidence
  - missing required fields

No second-guessing of pattern calls. The LLM is responsible for accuracy;
this layer only enforces shape + range.
"""

from __future__ import annotations

import math
from typing import Any


ALLOWED_PATTERN_TYPES: frozenset[str] = frozenset(
    {
        "horizontal_range",
        "trendline",
        "triangle",
        "flag_pennant",
        "head_shoulders",
    }
)
ALLOWED_DIRECTIONS: frozenset[str] = frozenset({"CALL", "PUT"})

REQUIRED_FIELDS: tuple[str, ...] = (
    "direction",
    "pattern_type",
    "trigger_price",
    "confidence",
)


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        if math.isfinite(f):
            return f
    return None


def validate_signals(
    signals: Any,
    today_close: float,
    max_distance_pct: float,
    min_confidence: float,
) -> list[dict[str, Any]]:
    """Filter the raw `signals` array from the LLM.

    Returns the list of valid signal dicts (each augmented with the same keys).
    The caller maps each to a `LeadCandidate`.
    """
    if not isinstance(signals, list):
        return []

    band_lo = today_close * (1.0 - max_distance_pct / 100.0)
    band_hi = today_close * (1.0 + max_distance_pct / 100.0)

    out: list[dict[str, Any]] = []
    for raw in signals:
        if not isinstance(raw, dict):
            continue
        if any(k not in raw for k in REQUIRED_FIELDS):
            continue

        direction = raw.get("direction")
        if direction not in ALLOWED_DIRECTIONS:
            continue

        pattern_type = raw.get("pattern_type")
        if pattern_type not in ALLOWED_PATTERN_TYPES:
            continue

        trigger_price = _finite_float(raw.get("trigger_price"))
        if trigger_price is None or trigger_price <= 0:
            continue
        if trigger_price < band_lo or trigger_price > band_hi:
            continue

        confidence = _finite_float(raw.get("confidence"))
        if confidence is None or confidence < min_confidence:
            continue
        confidence = max(0.0, min(1.0, confidence))

        rationale = raw.get("rationale")
        if not isinstance(rationale, str):
            rationale = ""

        out.append(
            {
                "direction": direction,
                "pattern_type": pattern_type,
                "trigger_price": trigger_price,
                "confidence": confidence,
                "rationale": rationale,
            }
        )

    return out
