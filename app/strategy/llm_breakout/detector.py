"""Detector orchestration: build prompt -> chat -> parse -> validate.

This is the per-instrument pipeline. The Strategy class (`LLMBreakoutStrategy`)
holds the per-run state (LLM client, settings, call counter) and calls
`detect_one()` once per instrument.

The detector is split from the strategy so it can be unit-tested without
spinning up the registry or the DB.

Every LLM call updates the persistent counters in `llm_breakout.health` so
the system health endpoint can report connectivity + last-call status.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from app.strategy.llm_breakout import health as llm_health
from app.strategy.llm_breakout.client import LLMClient
from app.strategy.llm_breakout.data_format import (
    build_user_prompt,
    compute_indicators,
    last_bar,
    slice_candles,
)
from app.strategy.llm_breakout.prompts import build_system_prompt
from app.strategy.llm_breakout.validator import validate_signals

log = logging.getLogger(__name__)


def _safe_float(x) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def detect_one(
    client: LLMClient,
    symbol: str,
    underlying_key: str,
    candles: pd.DataFrame,
    *,
    lookback_candles: int,
    volume_multiplier: float,
    divergence_pct: float,
    min_confidence: float,
) -> list[dict[str, Any]]:
    """Run the full per-instrument pipeline and return validated signals.

    Returns an empty list on:
      - too few candles to fill the lookback
      - LLM transport failure
      - malformed JSON / wrong shape
      - all signals rejected by the validator

    Never raises — the scheduler must not be crashed by a flaky LLM.
    """
    if candles is None or candles.empty:
        return []
    sliced = slice_candles(candles, lookback_candles)
    if sliced is None or sliced.empty or len(sliced) < 30:
        return []

    bar = last_bar(sliced)
    today_close = _safe_float(bar.get("close"))
    if today_close is None or today_close <= 0:
        return []

    system = build_system_prompt(
        lookback_candles=lookback_candles,
        volume_multiplier=volume_multiplier,
        divergence_pct=divergence_pct,
        min_confidence=min_confidence,
    )
    user = build_user_prompt(
        symbol=symbol,
        underlying_key=underlying_key,
        df=sliced,
        lookback=lookback_candles,
        volume_multiplier=volume_multiplier,
        divergence_pct=divergence_pct,
        min_confidence=min_confidence,
    )

    try:
        response = client.chat_json(system, user)
    except Exception as exc:  # noqa: BLE001 — never let a custom client raise past this point
        log.warning("llm_breakout: chat_json raised for %s: %s", symbol, exc)
        llm_health.record_error(f"chat_json raised: {exc}")
        return []

    if response is None:
        llm_health.record_error("empty response")
        return []
    if not isinstance(response, dict):
        llm_health.record_error("non-dict response")
        return []
    if response.get("_transport_error"):
        llm_health.record_error("transport error")
        return []

    raw_signals = response.get("signals", [])
    if not isinstance(raw_signals, list):
        llm_health.record_error("malformed signals payload")
        return []

    # Surface the model's reasoning text (chain-of-thought) so operators can
    # see *why* the LLM called something a breakout or passed. The client
    # stashes it under `_reasoning` when the model populates it.
    reasoning = response.get("_reasoning")
    if reasoning:
        # Truncate aggressively — most reasoning is huge; we just want a hint.
        snippet = str(reasoning).strip().replace("\n", " ")
        if len(snippet) > 600:
            snippet = snippet[:600] + "..."
        log.info("llm_breakout: %s reasoning: %s", symbol, snippet)

    valid = validate_signals(
        raw_signals,
        today_close=today_close,
        max_distance_pct=divergence_pct,
        min_confidence=min_confidence,
    )
    # The HTTP call succeeded — a validator rejection is not an LLM failure,
    # so we count this as a success regardless of whether any signal survived.
    llm_health.record_success()

    if not valid:
        return []

    log.info(
        "llm_breakout: %s -> %d signal(s) (trigger near %.2f)",
        symbol,
        len(valid),
        today_close,
    )
    return valid


def to_lead_components(signal: dict[str, Any]) -> dict[str, Any]:
    """Translate a validated LLM signal into the ComponentScores dict the
    downstream composite scorer expects."""
    confidence = float(signal["confidence"])
    volume_score = 1.0 if signal["volume_confirmed"] else 0.0
    return {
        "pattern_fit": confidence,
        "volume": volume_score,
        "trend_alignment": 0.5,  # unknown to LLM (no market-alignment feature today)
        "proximity": 1.0,        # LLM already filtered by divergence tolerance
        "structure": confidence,
        "extras": {"llm_rationale": signal.get("rationale", "")},
    }


def indicators_for_logging(candles: pd.DataFrame) -> dict[str, Any]:
    """Best-effort indicator snapshot for the lead's `meta` (debug visibility)."""
    sliced = slice_candles(candles, 250)
    return compute_indicators(sliced)
