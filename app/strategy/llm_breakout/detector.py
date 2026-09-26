"""Detector orchestration: build prompt -> agent loop -> validate.

This is the per-instrument pipeline. The Strategy class (`LLMBreakoutStrategy`)
holds the per-run state (LLM client, settings, call counter) and calls
`detect_one()` once per instrument.

The detector is split from the strategy so it can be unit-tested without
spinning up the registry or the DB.

`detect_one` first tries the TOOL-CALLING agent loop (`run_agent_loop`).
If the client doesn't expose `chat_with_tools` it falls back to a single
chat_json call so the legacy test stub continues to work.

Every LLM call updates the persistent counters in `llm_breakout.health` so
the system health endpoint can report connectivity + last-call status.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pandas as pd

from app.strategy.llm_breakout import health as llm_health
from app.strategy.llm_breakout.agent import run_agent_loop
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
    divergence_pct: float,
    min_confidence: float,
    broker=None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Run the full per-instrument pipeline and return validated signals.

    Returns an empty list on:
      - too few candles to fill the lookback
      - LLM transport failure
      - malformed JSON / wrong shape
      - all signals rejected by the validator
      - agent loop hits max iterations

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
        divergence_pct=divergence_pct,
        min_confidence=min_confidence,
    )
    user = build_user_prompt(
        symbol=symbol,
        underlying_key=underlying_key,
        df=sliced,
        lookback=lookback_candles,
        divergence_pct=divergence_pct,
        min_confidence=min_confidence,
    )

    context: dict[str, Any] = {
        "candles": sliced,
        "broker": broker,
        "today": today or date.today(),
        "lot_size": 1,
    }
    # Pass Bing key from config so news tool can choose API vs HTML scrape.
    try:
        from app.config import Config
        cfg = Config()
        context["bing_api_key"] = cfg.LLM_BING_API_KEY
        # Most underlyings use a known lot size hint from the Instrument row
        # (the lead generator passes it through `_run_for_instrument`). When
        # the detector is called without that hint (legacy tests), default to 1.
    except Exception:
        context["bing_api_key"] = ""

    # If the client supports tool calling, drive the agent loop. Otherwise
    # fall back to a single chat_json call (preserves the original pipeline
    # for legacy stub clients in tests).
    if hasattr(client, "chat_with_tools"):
        try:
            valid = run_agent_loop(
                client,
                system_prompt=system,
                user_prompt=user,
                context=context,
                today_close=today_close,
                divergence_pct=divergence_pct,
                min_confidence=min_confidence,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("llm_breakout: agent loop raised for %s: %s", symbol, exc)
            llm_health.record_error(f"agent loop raised: {exc}")
            return []
        if not valid:
            return []
        log.info(
            "llm_breakout: %s -> %d signal(s) (trigger near %.2f)",
            symbol, len(valid), today_close,
        )
        return valid

    # Legacy single-turn fallback.
    try:
        response = client.chat_json(system, user)
    except Exception as exc:  # noqa: BLE001
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
    reasoning = response.get("_reasoning")
    if reasoning:
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
    llm_health.record_success()
    return valid or []


def indicators_for_logging(candles: pd.DataFrame) -> dict[str, Any]:
    """Best-effort indicator snapshot for the lead's `meta` (debug visibility)."""
    sliced = slice_candles(candles, 250)
    return compute_indicators(sliced)
