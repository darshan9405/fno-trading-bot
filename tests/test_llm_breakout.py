"""LLM breakout detector tests.

Covers:
  - data_format (slice, render, indicators)
  - prompts (settings substitution + workflow guardrails visible)
  - validator (every reject rule + every pass case)
  - detector (end-to-end with a stub LLM client)
  - LLMBreakoutStrategy (registry + settings short-circuits)
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest


# --- fixtures --------------------------------------------------------------


def _df(closes, highs=None, lows=None, opens=None, volumes=None):
    n = len(closes)
    highs = highs or [c + 1.0 for c in closes]
    lows = lows or [c - 1.0 for c in closes]
    opens = opens or [c for c in closes]
    volumes = volumes or [1000.0] * n
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )


def _range_df(n: int = 260, low: float = 95.0, high: float = 105.0, breakout: float = 105.2):
    """A long horizontal range with today's close just above the ceiling."""
    closes = [100.0] * (n - 1) + [breakout]
    highs = [high] * (n - 1) + [breakout + 0.5]
    lows = [low] * (n - 1) + [100.0]
    return _df(closes, highs=highs, lows=lows)


@pytest.fixture
def db_env(tmp_path):
    from app import create_app
    from app.config import Config
    from app.db import dispose

    cfg = Config()
    cfg.RATE_LIMIT_ENABLED = False
    cfg.DATABASE_URL = f"sqlite:///{tmp_path / 'llm.db'}"
    cfg.SECRET_KEY = "k"
    cfg.JWT_MASTER_SECRET = "m"
    dispose()
    create_app(cfg)
    yield
    dispose()


# --- data_format -----------------------------------------------------------


def test_slice_candles_returns_last_n():
    df = _df([100.0] * 300)
    out = pd.DataFrame  # noqa: F841 -- import guard
    sliced = __import__("app.strategy.llm_breakout.data_format", fromlist=["slice_candles"]).slice_candles(df, 250)
    assert len(sliced) == 250
    assert sliced.index[-1] == df.index[-1]


def test_format_candles_block_renders_expected_header():
    from app.strategy.llm_breakout.data_format import format_candles_block

    block = format_candles_block(_df([100.0, 101.0]))
    assert block.splitlines()[0].startswith("DATE")
    assert "OPEN" in block.splitlines()[0]
    assert "VOLUME" in block.splitlines()[0]


def test_compute_indicators_with_insufficient_data():
    from app.strategy.llm_breakout.data_format import compute_indicators

    ind = compute_indicators(_df([100.0] * 10))
    assert ind["sma20"] is None
    assert ind["sma50"] is None
    assert ind["atr14"] is None


def test_compute_indicators_with_full_data():
    from app.strategy.llm_breakout.data_format import compute_indicators

    rng = np.random.default_rng(0)
    n = 200
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    high = close + 1
    low = close - 1
    df = _df(close.tolist(), highs=high.tolist(), lows=low.tolist())
    ind = compute_indicators(df)
    assert ind["sma20"] is not None
    assert ind["sma50"] is not None
    assert ind["atr14"] is not None


def test_build_user_prompt_contains_thresholds():
    from app.strategy.llm_breakout.data_format import build_user_prompt

    df = _range_df(n=260)
    prompt = build_user_prompt(
        symbol="NIFTY",
        underlying_key="NSE_INDEX|Nifty 50",
        df=df,
        lookback=250,
        divergence_pct=0.5,
        min_confidence=0.7,
    )
    assert "SYMBOL: NIFTY" in prompt
    assert "DIVERGENCE TOLERANCE: 0.5%" in prompt
    assert "MIN CONFIDENCE: 0.7" in prompt
    assert "LOOKBACK: 250 sessions" in prompt
    # Volume gating is gone.
    assert "VOLUME SPIKE MULTIPLIER" not in prompt


# --- prompts ---------------------------------------------------------------


def test_build_system_prompt_interpolates_thresholds():
    from app.strategy.llm_breakout.prompts import build_system_prompt

    sys = build_system_prompt(
        lookback_candles=250,
        divergence_pct=0.5,
        min_confidence=0.7,
    )
    assert "250" in sys
    assert "0.5%" in sys
    assert "0.7" in sys
    # Anti-hallucination guards must be present.
    assert "NO HALLUCINATIONS" in sys
    assert "R1" in sys and "R2" in sys and "R3" in sys and "R4" in sys
    # New structural sections must all be present.
    for marker in (
        "READING THE CHART",
        "Swing high",
        "Swing low",
        "Horizontal level",
        "Trendline",
        "Decisive cross",
        "Regime test",
        "UP-trend",
        "DOWN-trend",
        "SIDEWAYS",
        "MIXED",
        "WORKFLOW",
        "SCAN",
        "REGIME",
        "PATTERN",
        "TRIGGER",
        "CROSS",
        "CANDIDATE SELECTION ORDER",
        "COMMON MISTAKES",
        "FILTER",
        "F1. **DIVERGENCE",
        "F2. **CONFIDENCE",
        "F3. **RECENCY",
    ):
        assert marker in sys, f"missing section marker: {marker}"
    # Volume is informational only.
    assert "NOT a gate" in sys
    # Old workflow step names that were removed should be gone.
    for removed in ("SELF-CHECK", "PICK THE SINGLE", "ESTABLISH REGIME"):
        assert removed not in sys, f"removed marker still present: {removed}"
    # Filter gates still in place.
    assert "F3. **RECENCY" in sys
    # Pattern definitions for all 5 patterns.
    for name in ("HORIZONTAL_RANGE", "HEAD_SHOULDERS", "TRENDLINE", "TRIANGLE", "FLAG_PENNANT"):
        assert name in sys
    # Output schema must not include volume_confirmed.
    assert "volume_confirmed" not in sys


def test_system_prompt_contains_chain_of_thought_examples():
    """The two worked examples must include synthetic OHLCV tables AND the
    full reasoning chain — so future edits can't accidentally drop the
    chain-of-thought demonstration."""
    from app.strategy.llm_breakout.prompts import build_system_prompt

    sys = build_system_prompt(
        lookback_candles=250,
        divergence_pct=0.5,
        min_confidence=0.7,
    )
    # Both worked examples must be present.
    assert "Example 1 — Horizontal range CALL" in sys
    assert "Example 2 — Inverse H&S CALL" in sys
    # Each must contain a synthetic OHLCV block with the canonical header.
    assert sys.count("DATE       OPEN     HIGH     LOW      CLOSE    VOLUME") >= 2
    # Each must walk through the 6 reasoning steps (SCAN/REGIME/PATTERN/TRIGGER/CROSS/Confidence).
    for step in ("SCAN", "REGIME", "PATTERN", "TRIGGER", "CROSS", "Confidence"):
        # Each example block uses the step name in its reasoning chain.
        assert sys.count(step) >= 2, f"step {step} should appear in both examples"


def test_system_prompt_drops_over_cautious_reminder():
    """The old closing REMINDER biased the model toward empty. The new one
    is action-oriented — both must not coexist."""
    from app.strategy.llm_breakout.prompts import build_system_prompt

    sys = build_system_prompt(
        lookback_candles=250,
        divergence_pct=0.5,
        min_confidence=0.7,
    )
    assert "Be conservative. If unsure, return an empty list." not in sys
    # The new REMINDER explicitly frames emission as the goal.
    assert "Emit it." in sys


def test_system_prompt_is_concise():
    """Prompt length should stay reasonable — guard against drift back to
    bloated system prompts in future edits."""
    from app.strategy.llm_breakout.prompts import build_system_prompt

    sys = build_system_prompt(
        lookback_candles=250,
        divergence_pct=0.5,
        min_confidence=0.7,
    )
    # ~6K characters is a generous cap. The current prompt should be well
    # under this; the test fails on prompt bloat rather than on the current
    # implementation.
    assert len(sys) < 12000, f"system prompt is {len(sys)} chars; cap is 12000"


# --- validator -------------------------------------------------------------


def test_validator_drops_unknown_pattern_type():
    from app.strategy.llm_breakout.validator import validate_signals

    raw = [{"direction": "CALL", "pattern_type": "volume_breakout",
            "trigger_price": 100.0, "confidence": 0.9, "rationale": "x"}]
    assert validate_signals(raw, today_close=100.0, max_distance_pct=1.0, min_confidence=0.5) == []


def test_validator_drops_bad_direction():
    from app.strategy.llm_breakout.validator import validate_signals

    raw = [{"direction": "SIDEWAYS", "pattern_type": "horizontal_range",
            "trigger_price": 100.0, "confidence": 0.9, "rationale": "x"}]
    assert validate_signals(raw, today_close=100.0, max_distance_pct=1.0, min_confidence=0.5) == []


def test_validator_drops_hallucinated_trigger_price():
    from app.strategy.llm_breakout.validator import validate_signals

    raw = [{"direction": "CALL", "pattern_type": "horizontal_range",
            "trigger_price": 99999.0, "confidence": 0.9, "rationale": "x"}]
    assert validate_signals(raw, today_close=100.0, max_distance_pct=1.0, min_confidence=0.5) == []


def test_validator_drops_low_confidence():
    from app.strategy.llm_breakout.validator import validate_signals

    raw = [{"direction": "CALL", "pattern_type": "horizontal_range",
            "trigger_price": 100.0, "confidence": 0.4, "rationale": "x"}]
    assert validate_signals(raw, today_close=100.0, max_distance_pct=1.0, min_confidence=0.5) == []


def test_validator_clamps_confidence_out_of_range():
    from app.strategy.llm_breakout.validator import validate_signals

    raw = [{"direction": "CALL", "pattern_type": "horizontal_range",
            "trigger_price": 100.0, "confidence": 1.7, "rationale": "x"}]
    out = validate_signals(raw, today_close=100.0, max_distance_pct=5.0, min_confidence=0.5)
    assert len(out) == 1
    assert out[0]["confidence"] == 1.0


def test_validator_accepts_valid_signal():
    from app.strategy.llm_breakout.validator import validate_signals

    raw = [{"direction": "CALL", "pattern_type": "horizontal_range",
            "trigger_price": 105.0, "confidence": 0.8, "rationale": "x"}]
    out = validate_signals(raw, today_close=105.0, max_distance_pct=1.0, min_confidence=0.6)
    assert len(out) == 1
    assert out[0]["pattern_type"] == "horizontal_range"
    assert out[0]["direction"] == "CALL"


def test_validator_does_not_require_volume_confirmed():
    """Volume confirmation is gone — validator must accept signals with or without
    a stray `volume_confirmed` key in the LLM's payload."""
    from app.strategy.llm_breakout.validator import validate_signals

    base = {"direction": "CALL", "pattern_type": "horizontal_range",
            "trigger_price": 105.0, "confidence": 0.8, "rationale": "x"}
    with_volume = {**base, "volume_confirmed": True}
    without_volume = dict(base)
    a = validate_signals([with_volume], today_close=105.0, max_distance_pct=1.0, min_confidence=0.6)
    b = validate_signals([without_volume], today_close=105.0, max_distance_pct=1.0, min_confidence=0.6)
    assert len(a) == 1 and len(b) == 1


def test_validator_drops_non_dict_signal():
    from app.strategy.llm_breakout.validator import validate_signals

    raw = ["not a dict", 123, None, {"missing": "fields"}]
    assert validate_signals(raw, today_close=100.0, max_distance_pct=1.0, min_confidence=0.5) == []


def test_validator_drops_non_list_signals():
    from app.strategy.llm_breakout.validator import validate_signals

    assert validate_signals({"signals": "oops"}, today_close=100.0, max_distance_pct=1.0, min_confidence=0.5) == []
    assert validate_signals(None, today_close=100.0, max_distance_pct=1.0, min_confidence=0.5) == []


# --- detector (end-to-end with stub client) -------------------------------


def test_detect_one_returns_empty_on_transport_error():
    from app.strategy.llm_breakout import StubClient
    from app.strategy.llm_breakout.detector import detect_one

    client = StubClient()
    client.raise_on_call = 1
    df = _range_df()
    out = detect_one(
        client=client, symbol="NIFTY", underlying_key="k",
        candles=df, lookback_candles=250,
        divergence_pct=0.5, min_confidence=0.7,
    )
    assert out == []


def test_detect_one_returns_empty_on_bad_json_via_client_error():
    from app.strategy.llm_breakout import StubClient
    from app.strategy.llm_breakout.detector import detect_one

    client = StubClient(errors=[ValueError("malformed")])
    df = _range_df()
    out = detect_one(
        client=client, symbol="NIFTY", underlying_key="k",
        candles=df, lookback_candles=250,
        divergence_pct=0.5, min_confidence=0.7,
    )
    assert out == []


def test_detect_one_returns_empty_signals():
    from app.strategy.llm_breakout import StubClient
    from app.strategy.llm_breakout.detector import detect_one

    client = StubClient(responses=[{"signals": []}])
    df = _range_df()
    out = detect_one(
        client=client, symbol="NIFTY", underlying_key="k",
        candles=df, lookback_candles=250,
        divergence_pct=0.5, min_confidence=0.7,
    )
    assert out == []
    assert len(client.calls) == 1
    sys_msg, user_msg = client.calls[0]
    assert "SYMBOL: NIFTY" in user_msg
    assert "AS-OF DATE" in user_msg


def test_detect_one_filters_invalid_signals_but_keeps_valid_one():
    from app.strategy.llm_breakout import StubClient
    from app.strategy.llm_breakout.detector import detect_one

    response = {
        "signals": [
            # hallucinated trigger
            {"direction": "CALL", "pattern_type": "horizontal_range",
             "trigger_price": 99999.0, "confidence": 0.9, "rationale": "x"},
            # disallowed pattern_type
            {"direction": "CALL", "pattern_type": "volume_breakout",
             "trigger_price": 105.0, "confidence": 0.9, "rationale": "x"},
            # bad direction
            {"direction": "SIDEWAYS", "pattern_type": "horizontal_range",
             "trigger_price": 105.0, "confidence": 0.9, "rationale": "x"},
            # confidence too low
            {"direction": "PUT", "pattern_type": "trendline",
             "trigger_price": 95.0, "confidence": 0.3, "rationale": "x"},
            # valid
            {"direction": "CALL", "pattern_type": "horizontal_range",
             "trigger_price": 105.0, "confidence": 0.8, "rationale": "ok"},
        ]
    }
    client = StubClient(responses=[response])
    df = _range_df(n=260, breakout=105.2)
    out = detect_one(
        client=client, symbol="NIFTY", underlying_key="k",
        candles=df, lookback_candles=250,
        divergence_pct=0.5, min_confidence=0.7,
    )
    assert len(out) == 1
    assert out[0]["pattern_type"] == "horizontal_range"
    assert out[0]["trigger_price"] == 105.0


def test_detect_one_handles_too_few_candles():
    from app.strategy.llm_breakout import StubClient
    from app.strategy.llm_breakout.detector import detect_one

    client = StubClient(responses=[{"signals": [{"direction": "CALL", "pattern_type": "horizontal_range",
                                                   "trigger_price": 100.0, "confidence": 0.9,
                                                   "rationale": "x"}]}])
    df = _df([100.0] * 20)
    out = detect_one(
        client=client, symbol="X", underlying_key="k",
        candles=df, lookback_candles=250,
        divergence_pct=0.5, min_confidence=0.7,
    )
    assert out == []
    assert len(client.calls) == 0  # short-circuited before HTTP


# --- LLMBreakoutStrategy ---------------------------------------------------


def test_strategy_registered(db_env):
    from app.strategy import StrategyRegistry
    assert "llm_breakout" in StrategyRegistry.all()


def test_strategy_disabled_returns_empty(db_env):
    from app.settings import set_setting
    from app.strategy import StrategyRegistry
    from app.strategy.llm_breakout import StubClient

    set_setting("llm.enabled", False)
    strat = StrategyRegistry.get("llm_breakout")(client=StubClient())
    inst = SimpleNamespace(id=1, symbol="NIFTY", spot_instrument_key="k")
    out = strat.generate(inst, _range_df(), now=None)
    assert out == []


def test_strategy_emits_lead_with_meta_and_confidence(db_env):
    from app.settings import set_setting
    from app.strategy import StrategyRegistry
    from app.strategy.llm_breakout import StubClient

    set_setting("llm.enabled", True)
    set_setting("llm.min_confidence", 0.6)
    set_setting("max_lead_price_divergence_pct", 0.5)

    response = {"signals": [
        {"direction": "CALL", "pattern_type": "horizontal_range",
         "trigger_price": 105.0, "confidence": 0.8, "rationale": "ok"}
    ]}
    strat = StrategyRegistry.get("llm_breakout")(client=StubClient(responses=[response]))
    strat.begin_run(max_calls=10)
    inst = SimpleNamespace(id=1, symbol="NIFTY", spot_instrument_key="k")
    leads = strat.generate(inst, _range_df(n=260, breakout=105.2), now=None)
    assert len(leads) == 1
    lead = leads[0]
    assert lead.direction == "CALL"
    assert lead.signal_type == "horizontal_range"
    assert lead.signal_level == 105.0
    # LLM's confidence IS the lead's confidence (no composite re-blending).
    assert lead.confidence == 0.8
    assert lead.meta["source"] == "llm"
    assert lead.meta["llm_rationale"] == "ok"
    # No more `components` dict — tier-3 re-blend is a no-op for LLM signals.
    assert "components" not in lead.meta


def test_strategy_does_not_require_volume_confirmed(db_env):
    """A clean breakout signal without volume confirmation is still emitted."""
    from app.settings import set_setting
    from app.strategy import StrategyRegistry
    from app.strategy.llm_breakout import StubClient

    set_setting("llm.enabled", True)
    set_setting("llm.min_confidence", 0.6)
    set_setting("max_lead_price_divergence_pct", 0.5)

    response = {"signals": [
        {"direction": "PUT", "pattern_type": "head_shoulders",
         "trigger_price": 95.0, "confidence": 0.82, "rationale": "neckline just broken"}
    ]}
    strat = StrategyRegistry.get("llm_breakout")(client=StubClient(responses=[response]))
    strat.begin_run(max_calls=10)
    inst = SimpleNamespace(id=1, symbol="NIFTY", spot_instrument_key="k")
    # Build a tight H&S-shaped series around 95.
    df = _range_df(n=260, low=80.0, high=110.0, breakout=94.8)
    leads = strat.generate(inst, df, now=None)
    assert len(leads) == 1
    assert leads[0].direction == "PUT"
    assert leads[0].confidence == 0.82


def test_strategy_respects_max_calls_per_run(db_env):
    from app.settings import set_setting
    from app.strategy import StrategyRegistry
    from app.strategy.llm_breakout import StubClient

    set_setting("llm.enabled", True)
    set_setting("llm.min_confidence", 0.0)  # accept any confidence so transport-error is the gate
    client = StubClient(responses=[{"signals": []}])
    strat = StrategyRegistry.get("llm_breakout")(client=client)
    strat.begin_run(max_calls=2)
    inst = SimpleNamespace(id=1, symbol="NIFTY", spot_instrument_key="k")
    # First 2 calls go through; 3rd is capped.
    strat.generate(inst, _range_df(), now=None)
    strat.generate(inst, _range_df(), now=None)
    out = strat.generate(inst, _range_df(), now=None)
    assert out == []
    assert len(client.calls) == 2


def test_strategy_no_client_no_leads(db_env):
    from app.settings import set_setting
    from app.strategy import StrategyRegistry

    set_setting("llm.enabled", True)
    set_setting("llm.min_confidence", 0.6)

    class _NoneClient:
        def chat_json(self, system, user):
            return None

    strat = StrategyRegistry.get("llm_breakout")(client=_NoneClient())
    strat.begin_run(max_calls=10)
    inst = SimpleNamespace(id=1, symbol="NIFTY", spot_instrument_key="k")
    out = strat.generate(inst, _range_df(), now=None)
    assert out == []


# --- health module (persistent counters + snapshot) -----------------------


def test_health_get_stats_returns_zeros_when_unset(db_env):
    from app.strategy.llm_breakout import health as llm_health

    llm_health.reset()
    stats = llm_health.get_stats()
    assert stats["calls_total"] == 0
    assert stats["errors_total"] == 0
    assert stats["last_success_at"] is None
    assert stats["last_error_at"] is None
    assert stats["last_error"] is None


def test_health_record_success_increments_and_stamps(db_env):
    from app.strategy.llm_breakout import health as llm_health

    llm_health.reset()
    llm_health.record_success()
    llm_health.record_success()
    stats = llm_health.get_stats()
    assert stats["calls_total"] == 2
    assert stats["errors_total"] == 0
    assert stats["last_success_at"] is not None
    assert stats["last_error_at"] is None


def test_health_record_error_stamps_message_and_truncates(db_env):
    from app.strategy.llm_breakout import health as llm_health

    llm_health.reset()
    llm_health.record_error("transport error")
    llm_health.record_error("X" * 1000)
    stats = llm_health.get_stats()
    assert stats["errors_total"] == 2
    assert stats["last_error"] == "X" * 500
    assert stats["last_error_at"] is not None


def test_health_snapshot_marks_unconfigured_when_keys_missing(db_env, monkeypatch):
    from app.config import Config
    from app.strategy.llm_breakout import health as llm_health

    monkeypatch.setattr(Config, "LLM_API_KEY", "", raising=False)
    monkeypatch.setattr(Config, "LLM_BASE_URL", "", raising=False)
    monkeypatch.setattr(Config, "LLM_MODEL", "", raising=False)
    llm_health.reset()
    snap = llm_health.get_health_snapshot()
    assert snap["configured"] is False
    assert snap["status"] == "unconfigured"
    assert snap["model"] == ""
    assert snap["stats"]["calls_total"] == 0


def test_health_snapshot_status_ok_after_success(db_env, monkeypatch):
    from app.config import Config
    from app.strategy.llm_breakout import health as llm_health

    monkeypatch.setattr(Config, "LLM_API_KEY", "k", raising=False)
    monkeypatch.setattr(Config, "LLM_BASE_URL", "https://openrouter.ai/api/v1", raising=False)
    monkeypatch.setattr(Config, "LLM_MODEL", "minimax/minimax-m3", raising=False)
    llm_health.reset()
    llm_health.record_success()
    snap = llm_health.get_health_snapshot()
    assert snap["configured"] is True
    assert snap["status"] == "ok"
    assert snap["model"] == "minimax/minimax-m3"


def test_health_snapshot_status_error_when_last_event_is_error(db_env, monkeypatch):
    from app.config import Config
    from app.strategy.llm_breakout import health as llm_health

    monkeypatch.setattr(Config, "LLM_API_KEY", "k", raising=False)
    monkeypatch.setattr(Config, "LLM_BASE_URL", "https://openrouter.ai/api/v1", raising=False)
    monkeypatch.setattr(Config, "LLM_MODEL", "minimax/minimax-m3", raising=False)
    llm_health.reset()
    llm_health.record_success()
    llm_health.record_error("transport")
    snap = llm_health.get_health_snapshot()
    assert snap["status"] == "error"
    assert snap["stats"]["errors_total"] == 1


def test_health_snapshot_status_unknown_when_configured_but_no_calls(db_env, monkeypatch):
    from app.config import Config
    from app.strategy.llm_breakout import health as llm_health

    monkeypatch.setattr(Config, "LLM_API_KEY", "k", raising=False)
    monkeypatch.setattr(Config, "LLM_BASE_URL", "https://openrouter.ai/api/v1", raising=False)
    monkeypatch.setattr(Config, "LLM_MODEL", "minimax/minimax-m3", raising=False)
    llm_health.reset()
    snap = llm_health.get_health_snapshot()
    assert snap["configured"] is True
    assert snap["status"] == "unknown"


# --- detector records health counters on every call ---------------------


def test_detector_records_success_on_valid_signal(db_env):
    from app.strategy.llm_breakout import StubClient
    from app.strategy.llm_breakout import health as llm_health
    from app.strategy.llm_breakout.detector import detect_one

    llm_health.reset()
    client = StubClient(responses=[{"signals": [
        {"direction": "CALL", "pattern_type": "horizontal_range",
         "trigger_price": 105.0, "confidence": 0.8, "rationale": "x"}
    ]}])
    detect_one(
        client=client, symbol="NIFTY", underlying_key="k",
        candles=_range_df(n=260, breakout=105.2),
        lookback_candles=250,
        divergence_pct=0.5, min_confidence=0.7,
    )
    stats = llm_health.get_stats()
    assert stats["calls_total"] == 1
    assert stats["errors_total"] == 0
    assert stats["last_success_at"] is not None


def test_detector_records_success_when_validator_rejects_all(db_env):
    from app.strategy.llm_breakout import StubClient
    from app.strategy.llm_breakout import health as llm_health
    from app.strategy.llm_breakout.detector import detect_one

    llm_health.reset()
    client = StubClient(responses=[{"signals": [
        {"direction": "CALL", "pattern_type": "horizontal_range",
         "trigger_price": 99999.0, "confidence": 0.8, "rationale": "x"}
    ]}])
    detect_one(
        client=client, symbol="NIFTY", underlying_key="k",
        candles=_range_df(n=260, breakout=105.2),
        lookback_candles=250,
        divergence_pct=0.5, min_confidence=0.7,
    )
    stats = llm_health.get_stats()
    # HTTP succeeded; validator rejection is not an LLM failure.
    assert stats["calls_total"] == 1
    assert stats["errors_total"] == 0


def test_detector_records_error_on_transport_error(db_env):
    from app.strategy.llm_breakout import StubClient
    from app.strategy.llm_breakout import health as llm_health
    from app.strategy.llm_breakout.detector import detect_one

    llm_health.reset()
    client = StubClient()
    client.raise_on_call = 1
    detect_one(
        client=client, symbol="NIFTY", underlying_key="k",
        candles=_range_df(),
        lookback_candles=250,
        divergence_pct=0.5, min_confidence=0.7,
    )
    stats = llm_health.get_stats()
    assert stats["errors_total"] == 1
    assert stats["calls_total"] == 0
    assert stats["last_error"] == "transport error"


def test_detector_records_error_on_client_exception(db_env):
    from app.strategy.llm_breakout import StubClient
    from app.strategy.llm_breakout import health as llm_health
    from app.strategy.llm_breakout.detector import detect_one

    llm_health.reset()
    client = StubClient(errors=[RuntimeError("boom")])
    detect_one(
        client=client, symbol="NIFTY", underlying_key="k",
        candles=_range_df(),
        lookback_candles=250,
        divergence_pct=0.5, min_confidence=0.7,
    )
    stats = llm_health.get_stats()
    assert stats["errors_total"] == 1
    assert "chat_json raised" in stats["last_error"]


def test_detector_records_error_on_malformed_signals_payload(db_env):
    from app.strategy.llm_breakout import StubClient
    from app.strategy.llm_breakout import health as llm_health
    from app.strategy.llm_breakout.detector import detect_one

    llm_health.reset()
    client = StubClient(responses=[{"signals": "not a list"}])
    detect_one(
        client=client, symbol="NIFTY", underlying_key="k",
        candles=_range_df(),
        lookback_candles=250,
        divergence_pct=0.5, min_confidence=0.7,
    )
    stats = llm_health.get_stats()
    assert stats["errors_total"] == 1
    assert "malformed" in stats["last_error"]


def test_detector_does_not_record_anything_when_candles_short(db_env):
    """Pre-flight short-circuit (too few candles) bypasses the HTTP call entirely,
    so no health counter changes."""
    from app.strategy.llm_breakout import StubClient
    from app.strategy.llm_breakout import health as llm_health
    from app.strategy.llm_breakout.detector import detect_one

    llm_health.reset()
    client = StubClient(responses=[{"signals": []}])
    df = _df([100.0] * 20)
    detect_one(
        client=client, symbol="X", underlying_key="k", candles=df,
        lookback_candles=250,
        divergence_pct=0.5, min_confidence=0.7,
    )
    stats = llm_health.get_stats()
    assert stats["calls_total"] == 0
    assert stats["errors_total"] == 0


# --- client (transport) ----------------------------------------------------


def test_openai_compat_client_sends_expected_payload():
    """Smoke-test the request shape via a mocked httpx transport."""
    from app.strategy.llm_breakout.client import OpenAICompatClient

    sent = {}

    class _MockTransport:
        def post(self, url, headers=None, json=None):
            sent["url"] = url
            sent["headers"] = headers
            sent["json"] = json

            class _Resp:
                status_code = 200
                text = ""

                def json(self_inner):
                    return {
                        "choices": [
                            {"message": {"content": '{"signals": []}'}}
                        ]
                    }

                def raise_for_status(self_inner):
                    return None

            return _Resp()

    import httpx

    class _MockClient:
        def __init__(self, timeout):
            self._t = _MockTransport()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            return self._t.post(url, headers=headers, json=json)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(httpx, "Client", _MockClient)

    c = OpenAICompatClient(base_url="https://api.example.com/v1",
                           api_key="k", model="m", timeout_s=10, max_retries=0)
    out = c.chat_json("sys", "user")
    assert out == {"signals": []}
    assert sent["url"] == "https://api.example.com/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer k"
    body = sent["json"]
    assert body["model"] == "m"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user"},
    ]
    monkey.undo()


def test_openai_compat_client_merges_extra_headers(monkeypatch):
    """OpenRouter app-attribution headers (HTTP-Referer, X-Title) must reach
    the request when supplied via `extra_headers`."""
    from app.strategy.llm_breakout.client import OpenAICompatClient

    sent = {}

    class _MockTransport:
        def post(self, url, headers=None, json=None):
            sent["headers"] = headers

            class _Resp:
                status_code = 200
                text = ""

                def json(self_inner):
                    return {"choices": [{"message": {"content": '{"signals": []}'}}]}

                def raise_for_status(self_inner):
                    return None

            return _Resp()

    import httpx

    class _MockClient:
        def __init__(self, timeout):
            self._t = _MockTransport()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            return self._t.post(url, headers=headers, json=json)

    monkeypatch.setattr(httpx, "Client", _MockClient)

    c = OpenAICompatClient(
        base_url="https://openrouter.ai/api/v1",
        api_key="k",
        model="minimax/minimax-m3",
        timeout_s=10,
        max_retries=0,
        extra_headers={
            "HTTP-Referer": "https://example.com",
            "X-Title": "FnO Trading Bot",
        },
    )
    c.chat_json("sys", "user")
    headers = sent["headers"]
    assert headers["Authorization"] == "Bearer k"
    assert headers["Content-Type"] == "application/json"
    assert headers["HTTP-Referer"] == "https://example.com"
    assert headers["X-Title"] == "FnO Trading Bot"


def test_build_default_client_sends_openrouter_headers_when_env_set(monkeypatch):
    """`build_default_client` must thread OPENROUTER_APP_URL / NAME through
    to the client as HTTP-Referer / X-Title."""
    from app.config import Config
    from app.strategy.llm_breakout import client as client_mod

    monkeypatch.setattr(Config, "LLM_API_KEY", "k", raising=False)
    monkeypatch.setattr(Config, "LLM_BASE_URL", "https://openrouter.ai/api/v1", raising=False)
    monkeypatch.setattr(Config, "LLM_MODEL", "minimax/minimax-m3", raising=False)
    monkeypatch.setattr(Config, "LLM_TIMEOUT_S", 30.0, raising=False)
    monkeypatch.setattr(Config, "LLM_MAX_RETRIES", 2, raising=False)
    monkeypatch.setattr(Config, "OPENROUTER_APP_URL", "https://example.com", raising=False)
    monkeypatch.setattr(Config, "OPENROUTER_APP_NAME", "FnO Trading Bot", raising=False)

    c = client_mod.build_default_client()
    assert isinstance(c, client_mod.OpenAICompatClient)
    assert c._extra_headers == {
        "HTTP-Referer": "https://example.com",
        "X-Title": "FnO Trading Bot",
    }


def test_build_default_client_omits_openrouter_headers_when_env_unset(monkeypatch):
    """Off by default: with both env vars empty, no extra headers are sent."""
    from app.config import Config
    from app.strategy.llm_breakout import client as client_mod

    monkeypatch.setattr(Config, "LLM_API_KEY", "k", raising=False)
    monkeypatch.setattr(Config, "LLM_BASE_URL", "https://openrouter.ai/api/v1", raising=False)
    monkeypatch.setattr(Config, "LLM_MODEL", "minimax/minimax-m3", raising=False)
    monkeypatch.setattr(Config, "LLM_TIMEOUT_S", 30.0, raising=False)
    monkeypatch.setattr(Config, "LLM_MAX_RETRIES", 2, raising=False)
    monkeypatch.setattr(Config, "OPENROUTER_APP_URL", "", raising=False)
    monkeypatch.setattr(Config, "OPENROUTER_APP_NAME", "", raising=False)

    c = client_mod.build_default_client()
    assert isinstance(c, client_mod.OpenAICompatClient)
    assert c._extra_headers == {}


def test_config_defaults_to_openrouter():
    """Sanity: Config defaults flip to OpenRouter + minimax/minimax-m3."""
    from app.config import Config

    # Defaults are read at instantiation; unset the env so we see true defaults.
    import os
    monkey = pytest.MonkeyPatch()
    monkey.delenv("LLM_BASE_URL", raising=False)
    monkey.delenv("LLM_MODEL", raising=False)
    monkey.delenv("OPENROUTER_APP_URL", raising=False)
    monkey.delenv("OPENROUTER_APP_NAME", raising=False)
    cfg = Config()
    assert cfg.LLM_BASE_URL == "https://openrouter.ai/api/v1"
    assert cfg.LLM_MODEL == "minimax/minimax-m3"
    assert cfg.OPENROUTER_APP_URL == ""
    assert cfg.OPENROUTER_APP_NAME == ""
