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
from typing import Any, Callable

import pandas as pd

from app.strategy.llm_breakout import health as llm_health
from app.strategy.llm_breakout.agent import AgentResult, run_agent_loop
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


def _empty_result(error: str | None = None) -> AgentResult:
    """Build an empty `AgentResult` for early-out paths."""
    return AgentResult(signals=[], error=error)


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
    on_tool_call: Callable[[dict[str, Any]], None] | None = None,
) -> AgentResult:
    """Run the full per-instrument pipeline and return an ``AgentResult``.

    Returns an ``AgentResult`` even on early-out paths (no candles, blank
    close, transport failure, parse failure, validator rejection, max
    iterations). ``AgentResult.signals`` may be empty; callers should
    check ``AgentResult.ok`` to distinguish "loop bailed" from "model
    just didn't find a setup" (the latter still carries a
    ``rejection_reason``).

    Never raises — the scheduler must not be crashed by a flaky LLM.

    `on_tool_call(event)` is forwarded into the agent loop so the lead
    generator can stream per-tool-call progress to the UI.
    """
    if candles is None or candles.empty:
        return _empty_result()
    sliced = slice_candles(candles, lookback_candles)
    if sliced is None or sliced.empty or len(sliced) < 30:
        return _empty_result()

    bar = last_bar(sliced)
    today_close = _safe_float(bar.get("close"))
    if today_close is None or today_close <= 0:
        return _empty_result()

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
    # Most underlyings use a known lot size hint from the Instrument row
    # (the lead generator passes it through `_run_for_instrument`). When
    # the detector is called without that hint (legacy tests), default to 1.

    # If the client supports tool calling, drive the agent loop. Otherwise
    # fall back to a single chat_json call (preserves the original pipeline
    # for legacy stub clients in tests).
    if hasattr(client, "chat_with_tools"):
        try:
            result = run_agent_loop(
                client,
                system_prompt=system,
                user_prompt=user,
                context=context,
                today_close=today_close,
                divergence_pct=divergence_pct,
                min_confidence=min_confidence,
                on_tool_call=on_tool_call,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("llm_breakout: agent loop raised for %s: %s", symbol, exc)
            llm_health.record_error(f"agent loop raised: {exc}")
            return _empty_result(f"agent loop raised: {exc}")
        if not result.signals:
            return result
        log.info(
            "llm_breakout: %s -> %d signal(s) (trigger near %.2f)",
            symbol, len(result.signals), today_close,
        )
        return result

    # Legacy single-turn fallback. Wraps everything in an AgentResult so
    # the strategy / lead generator has the same shape regardless of which
    # path was taken. We don't have tool calls here, just the final
    # message.
    try:
        response = client.chat_json(system, user)
    except Exception as exc:  # noqa: BLE001
        log.warning("llm_breakout: chat_json raised for %s: %s", symbol, exc)
        llm_health.record_error(f"chat_json raised: {exc}")
        return _empty_result(f"chat_json raised: {exc}")
    if response is None:
        llm_health.record_error("empty response")
        return _empty_result("empty response")
    if not isinstance(response, dict):
        llm_health.record_error("non-dict response")
        return _empty_result("non-dict response")
    if response.get("_transport_error"):
        llm_health.record_error("transport error")
        return _empty_result("transport error")
    raw_signals = response.get("signals", [])
    if not isinstance(raw_signals, list):
        llm_health.record_error("malformed signals payload")
        return _empty_result("malformed signals payload")
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
    # The legacy path doesn't go through the agent loop, so there's no
    # tool-call log to surface. Rejection_reason comes from the model's
    # payload when present (newer prompts include it). Cap at 32 KB so
    # the UI can render the full LLM explanation verbatim. Short_reason
    # is the single-sentence UI summary (≤ 200 chars) the Leads table
    # shows verbatim.
    rejection_reason = response.get("rejection_reason")
    if not rejection_reason and not raw_signals:
        reasoning_s = (reasoning or "").strip()
        if reasoning_s:
            rejection_reason = reasoning_s[:32_000]
    short_reason = response.get("short_reason")
    if not short_reason and not raw_signals:
        snippet = (rejection_reason or reasoning or "").strip().replace("\n", " ")
        if snippet:
            short_reason = snippet[:200]
    if not short_reason and raw_signals:
        try:
            first = raw_signals[0] if isinstance(raw_signals, list) else None
            if isinstance(first, dict):
                d = str(first.get("direction") or "").strip()
                p = str(first.get("pattern_type") or "").strip().replace("_", " ")
                r = str(first.get("rationale") or "").strip().replace("\n", " ")
                if d and p and r:
                    short_reason = f"{d} {p}: {r}"[:200]
                elif d and p:
                    short_reason = f"{d} {p} breakout"[:200]
        except Exception:
            pass
    return AgentResult(
        signals=valid,
        short_reason=str(short_reason)[:200] if short_reason else None,
        rejection_reason=str(rejection_reason)[:32_000] if rejection_reason else None,
        tool_calls=[],
        agent_iters=0,
        agent_duration_s=0.0,
        rationale=reasoning,
        error=None,
    )


def indicators_for_logging(candles: pd.DataFrame) -> dict[str, Any]:
    """Best-effort indicator snapshot for the lead's `meta` (debug visibility)."""
    sliced = slice_candles(candles, 250)
    return compute_indicators(sliced)
