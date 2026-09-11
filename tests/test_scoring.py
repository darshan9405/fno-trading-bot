"""Composite scoring pipeline tests.

Covers `app.strategy.scoring` (composite math, weight maps, soft-penalty),
`app.services.score_decay` (staleness curve), and `app.services.calibration`
(historical win-rate gating + multiplier).
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.strategy.scoring import (
    ComponentScores,
    DEFAULT_TIER3_WEIGHTS,
    DEFAULT_WEIGHTS,
    available_weight_map,
    composite,
)
from app.strategy.breakout.signals import PatternSignal
from app.strategy.breakout.detector import rank_signals
from app.services.score_decay import decay_factor, decayed_score


# --- Pure math -------------------------------------------------------------


def test_default_weights_sum_to_one():
    assert pytest.approx(sum(DEFAULT_WEIGHTS.values()), abs=1e-9) == 1.0


def test_composite_blends_components():
    c = ComponentScores(pattern_fit=0.8, volume=0.6, trend_alignment=0.9, proximity=1.0, structure=0.7)
    s = composite(c)
    expected = 0.8 * 0.40 + 0.6 * 0.25 + 0.9 * 0.15 + 1.0 * 0.10 + 0.7 * 0.10
    # No soft-penalty trigger (floor = min(0.8, 0.6, 0.9) = 0.6 >= 0.4)
    assert s == pytest.approx(round(expected, 4))


def test_composite_clamps_to_unit_range():
    # Make sure out-of-range inputs can't push the result above 1.0 even if
    # a caller mis-calibrates a dimension.
    c = ComponentScores(pattern_fit=1.5, volume=2.0, trend_alignment=1.0, proximity=1.0, structure=1.0)
    assert composite(c) <= 1.0


def test_composite_soft_penalty_kicks_in_below_threshold():
    # Floor < 0.4 triggers 0.85 multiplier on the sum.
    pure_no_floor = ComponentScores(pattern_fit=0.8, volume=0.1, trend_alignment=0.9, proximity=0.5, structure=0.5)
    raw_score = (0.8 * 0.40 + 0.1 * 0.25 + 0.9 * 0.15 + 0.5 * 0.10 + 0.5 * 0.10) / 1.0
    expected = round(raw_score * 0.85, 4)
    assert composite(pure_no_floor) == pytest.approx(expected)


def test_available_weight_map_rescales_when_tier3_on():
    base = sum(available_weight_map().values())
    with_iv = available_weight_map(enable_iv=True)
    with_oi = available_weight_map(enable_oi=True)
    full = available_weight_map(enable_iv=True, enable_oi=True, enable_tod=True)
    assert pytest.approx(base, abs=1e-9) == 1.0
    assert pytest.approx(sum(with_iv.values()), abs=1e-9) == 1.0
    assert pytest.approx(sum(with_oi.values()), abs=1e-9) == 1.0
    assert pytest.approx(sum(full.values()), abs=1e-9) == 1.0


def test_available_weight_map_includes_tier3_dimensions():
    weight_map = available_weight_map(enable_iv=True, enable_oi=True, enable_tod=True)
    for key in DEFAULT_TIER3_WEIGHTS:
        assert key in weight_map


def test_components_with_extra_merges_dict():
    c = ComponentScores(pattern_fit=0.5).with_extra(iv=0.7, oi=0.4)
    assert c.extras == {"iv": 0.7, "oi": 0.4}
    again = c.with_extra(iv=0.9)  # override
    assert again.extras["iv"] == 0.9
    assert again.extras["oi"] == 0.4  # previous value preserved


# --- PatternSignal + __post_init__ -----------------------------------------


def test_pattern_signal_preserves_confidence_when_no_evidence():
    # Synthetic edge case: caller hasn't filled components. We must NOT
    # overwrite the legacy `confidence` value (`__post_init__` is conservative).
    s = PatternSignal("CALL", "x", 100.0, 0.5)
    assert s.confidence == 0.5  # unchanged


def test_pattern_signal_recomputes_when_components_have_evidence():
    s = PatternSignal(
        "CALL", "horizontal_range", 100.0, 0.5,
        ComponentScores(pattern_fit=0.8, volume=0.7, trend_alignment=0.5, proximity=1.0, structure=0.8),
    )
    expected = (0.8 * 0.40 + 0.7 * 0.25 + 0.5 * 0.15 + 1.0 * 0.10 + 0.8 * 0.10)
    expected = round(expected, 4)
    # floor = min(0.8, 0.7, 0.5) = 0.5 >= 0.4 -> no soft-penalty
    assert s.confidence == pytest.approx(expected)


# --- rank_signals ---------------------------------------------------------


def test_rank_signals_returns_top_n():
    from app.strategy.breakout.signals import PatternSignal as Sig

    sigs = [
        Sig("CALL", "x", 100.0, 0.6),
        Sig("PUT", "y", 99.0, 0.9),
        Sig("PUT", "z", 98.0, 0.7),
    ]
    ranked = rank_signals(sigs, top_k=2, min_score=0.5)
    assert len(ranked) == 2
    assert ranked[0].confidence == 0.9
    assert ranked[1].confidence == 0.7


def test_rank_signals_filters_below_min_score():
    from app.strategy.breakout.signals import PatternSignal as Sig

    sigs = [Sig("CALL", "x", 100.0, 0.4), Sig("PUT", "y", 99.0, 0.9)]
    ranked = rank_signals(sigs, top_k=3, min_score=0.5)
    assert len(ranked) == 1
    assert ranked[0].direction == "PUT"


def test_rank_signals_top_k_1_equivalent_to_best_signal():
    from app.strategy.breakout.signals import PatternSignal as Sig

    from app.strategy.breakout.detector import best_signal

    sigs = [Sig("CALL", "x", 100.0, 0.4), Sig("PUT", "y", 99.0, 0.9)]
    ranked = rank_signals(sigs, top_k=1, min_score=0.5)
    assert best_signal(sigs, min_confidence=0.5) is not None
    assert ranked[0].direction == best_signal(sigs, min_confidence=0.5).direction


# --- score_decay ---------------------------------------------------------


class _StubLead:
    def __init__(self, confidence: float, created_at):
        self.confidence = confidence
        self.created_at = created_at


def test_decay_disabled_when_half_life_zero():
    now = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    lead = _StubLead(0.80, now - timedelta(hours=4))
    assert decayed_score(lead, half_life_min=0, now=now) == 0.80


def test_decay_factor_returns_one_for_zero_half_life():
    created = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    now = datetime(2026, 9, 4, 14, 0, tzinfo=timezone.utc)
    assert decay_factor(created, now, half_life_min=0.0) == 1.0


def test_decay_factor_halves_at_one_half_life():
    created = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    now = created + timedelta(minutes=30)
    factor = decay_factor(created, now, half_life_min=30)
    assert factor == pytest.approx(0.5, abs=0.02)


def test_decay_factor_floors_at_half():
    created = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    # 24h elapsed, 30min half-life -> extreme decay -> clamps to FLOOR (0.5)
    now = created + timedelta(hours=24)
    factor = decay_factor(created, now, half_life_min=30)
    assert factor == 0.5


def test_decay_handles_naive_created_at():
    """The Lead model stores created_at as naive UTC. Ensure no error."""
    lead = _StubLead(0.80, datetime(2026, 9, 4, 10, 0))  # naive
    now = datetime(2026, 9, 4, 10, 30, tzinfo=timezone.utc)
    factor = decay_factor(lead.created_at, now, half_life_min=60)
    assert 0.5 <= factor <= 1.0


def test_decayed_score_multiplies_confidence():
    created = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    lead = _StubLead(0.80, created)
    # 60 min elapsed, 60 min half-life -> factor 0.5 -> score 0.40
    now = created + timedelta(minutes=60)
    assert decayed_score(lead, half_life_min=60, now=now) == pytest.approx(0.40, abs=0.01)


# --- calibration_multiplier -----------------------------------------------


def test_calibration_multiplier_neutral_when_alpha_zero():
    from app.services.calibration import calibration_multiplier
    assert calibration_multiplier("x", "NSE_INDEX|Nifty 50", alpha=0.0) == 1.0


# --- end-to-end: detector emits components -------------------------------


def test_horizontal_detector_emits_components_and_confidence():
    import pandas as pd

    closes = [105.0] * 65 + [110.2]
    highs = [110.0] * 65 + [111.0]
    lows = [100.0] * 65 + [105.0]
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    df = pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": 1000.0},
        index=idx,
    )
    from app.strategy.breakout.horizontal import detect_horizontal

    signals = detect_horizontal(df, lookback=60, proximity_pct=0.5, min_touches=1)
    assert signals, "expected at least one horizontal_range signal"
    s = signals[0]
    assert s.direction == "CALL"
    assert s.signal_level == 110.0
    # Components populated (Tier-1 contract).
    assert s.components.pattern_fit > 0.0
    assert s.components.proximity == 1.0
    assert s.components.structure > 0.0
    # Confidence recomputed via composite in __post_init__.
    expected_pattern = 0.40 * s.components.pattern_fit + 0.25 * s.components.volume + \
                       0.15 * s.components.trend_alignment + 0.10 * s.components.proximity + \
                       0.10 * s.components.structure
    assert s.confidence == pytest.approx(round(expected_pattern, 4), abs=0.01)
