"""Staleness decay for queued leads.

A lead that has been sitting in the `queued` state for too long is increasingly
likely to be a false signal: the surrounding market context has moved, the
price has likely diverged from the trigger level, and the option premium has
likely widened or repriced. We let the order placer apply gentle exponential
decay so the best fresh signals win over stale ones, even when margin/limits
constrain how many we can place.

Decay is ONLY applied at rank / placement time so re-generating leads is
idempotent: the stored `confidence` always represents the score at the
moment of detection, and decay is recomputed whenever the order placer
runs. This matches the pre-existing `max_lead_price_divergence_pct` filter
which is also a "stale" check at the very last step.

Enable via:
    breakout.staleness_half_life_min = N  (minutes, 0 disables)
"""

from __future__ import annotations

import math
from datetime import datetime, timezone


DEFAULT_HALF_LIFE_MIN = 0.0  # disabled by default
FLOOR_FACTOR = 0.5           # never decay below 50% of the original score
MAX_DECAY_WINDOW_MIN = 8 * 60  # beyond 8h, treat as fully stale (clamped)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def decay_factor(created_at: datetime, now: datetime | None,
                 half_life_min: float) -> float:
    """Multiplicative decay factor for a queued lead.

    Returns 1.0 when `half_life_min <= 0` (disabled — factor is neutral).
    Otherwise applies `exp(-elapsed * ln(2) / half_life)`, clamped to
    [`FLOOR_FACTOR`, 1.0]. The score at placement time is
    `lead.confidence * decay_factor(...)`.

    `created_at` is naive UTC (matches `app.models.Lead.created_at` default);
    if `now` is tz-aware (IST from the scheduler), we convert both to UTC so
    the subtraction is well-defined.
    """
    if not half_life_min or half_life_min <= 0:
        return 1.0
    if created_at is None:
        return 1.0
    # Normalise both ends to UTC. created_at defaults to naive UTC; now is
    # typically tz-aware IST passed by the scheduler.
    if created_at.tzinfo is None:
        created_at_utc = created_at.replace(tzinfo=timezone.utc)
    else:
        created_at_utc = created_at.astimezone(timezone.utc)
    if now is None:
        now_utc = _now_utc()
    else:
        now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)

    elapsed_min = max(0.0, (now_utc - created_at_utc).total_seconds() / 60.0)
    elapsed_min = min(elapsed_min, MAX_DECAY_WINDOW_MIN)
    try:
        factor = math.exp(-elapsed_min * math.log(2) / float(half_life_min))
    except (ValueError, ZeroDivisionError):
        return 1.0
    return max(FLOOR_FACTOR, min(1.0, factor))


def decayed_score(lead, half_life_min: float, now: datetime | None = None) -> float:
    """Score at placement time = original confidence x decay factor."""
    base = float(getattr(lead, "confidence", 0.0) or 0.0)
    return round(base * decay_factor(getattr(lead, "created_at", None), now, half_life_min), 4)
