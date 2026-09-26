"""LLM-driven breakout strategy.

Replaces the math-based detectors (`app.strategy.breakout.*`, now removed)
with a single OpenAI-compatible chat call (default: direct MiniMax M3
endpoint) per instrument. The LLM is run in a TOOL-CALLING agent loop so
it can pull recent symbol news, fetch option-chain OI/IV context, and run
breakout-strength math (ATR, swing distance, R:R) before emitting its
final verdict. The app handles format conversion, structural validation,
and persistence.

Public surface:
  - LLMBreakoutStrategy  — registered as "llm_breakout"
  - LLMClient (Protocol) — injectable for tests

Settings (DB-backed, see `app.settings.DEFAULT_SETTINGS`):
  - llm.enabled            master switch
  - llm.lookback_candles   N daily bars to slice into the prompt (200-250)
  - llm.min_confidence     floor for LLM-reported confidence
  - llm.volume_multiplier  also passed to the LLM in the prompt
  - llm.temperature        model sampling temperature
  - llm.max_calls_per_run  hard cap so a slow LLM can't block the scheduler

Env (config.py):
  - LLM_API_KEY            MiniMax M3 API key (direct call, no middleman)
  - LLM_BASE_URL           defaults to https://api.minimax.io/v1
  - LLM_MODEL              defaults to "MiniMax-M3"
  - LLM_TIMEOUT_S          request timeout (seconds)
  - LLM_MAX_RETRIES        transport retries on 429/5xx
  - LLM_AGENT_MAX_ITERATIONS  cap on tool-call loop iterations
  - LLM_APP_URL            optional -> HTTP-Referer header
  - LLM_APP_NAME           optional -> X-Title header

The `fetch_news` tool hits Google's public News RSS (no API key). The old
`LLM_BING_API_KEY` env var was retired along with the Bing News Search API
on 2025-08-11 and has been removed from config.

Failure mode: any LLM error or empty result -> `generate()` returns `[]`.
No math fallback (the math detectors were removed). On any per-instrument
failure, the lead generator continues with the next instrument.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import pandas as pd

from app.strategy.base import LeadCandidate, Strategy, register_strategy
from app.strategy.llm_breakout.client import LLMClient, OpenAICompatClient
from app.strategy.llm_breakout.detector import (
    detect_one,
    indicators_for_logging,
)

log = logging.getLogger(__name__)


@dataclass
class _RunState:
    """Per-scheduler-tick state. `calls_used` resets when the run ends; for the
    scheduler we use a process-local counter that the lead_generator can
    reset by constructing a fresh strategy instance each tick (current
    behaviour)."""

    calls_used: int = 0
    cap: int = 0


@register_strategy("llm_breakout")
class LLMBreakoutStrategy(Strategy):
    name = "llm_breakout"
    required_interval = "day"

    def __init__(self, client: LLMClient | None = None) -> None:
        self._client_override = client
        self._run = _RunState()
        self._reset_lock = threading.Lock()

    def begin_run(self, max_calls: int) -> None:
        """Reset the per-run call counter. Called by the lead generator before
        iterating instruments."""
        with self._reset_lock:
            self._run = _RunState(cap=max_calls)

    def _client(self) -> LLMClient | None:
        if self._client_override is not None:
            return self._client_override
        from app.strategy.llm_breakout.client import build_default_client
        return build_default_client()

    def generate(self, instrument, candles: pd.DataFrame, now, *,
                 broker=None, today=None, lot_size: int | None = None,
                 on_tool_call=None,
                 on_scan_result=None) -> list[LeadCandidate]:
        from app.settings import get_setting

        if not bool(get_setting("llm.enabled", True)):
            return []

        lookback = int(get_setting("llm.lookback_candles", 250))
        min_conf = float(get_setting("llm.min_confidence", 0.7))
        divergence_pct = float(get_setting("max_lead_price_divergence_pct", 0.5))

        if self._run.cap > 0 and self._run.calls_used >= self._run.cap:
            return []

        client = self._client()
        if client is None:
            return []

        self._run.calls_used += 1

        try:
            agent_result = detect_one(
                client=client,
                symbol=getattr(instrument, "symbol", ""),
                underlying_key=getattr(instrument, "spot_instrument_key", ""),
                candles=candles,
                lookback_candles=lookback,
                divergence_pct=divergence_pct,
                min_confidence=min_conf,
                broker=broker,
                today=today,
                on_tool_call=on_tool_call,
            )
        except Exception as exc:  # noqa: BLE001 — never let an LLM error crash the tick
            log.exception("llm_breakout: detect_one raised for %s: %s",
                          getattr(instrument, "symbol", "?"), exc)
            # Best-effort: still hand the lead generator a structured
            # "this instrument errored out" payload so the Scanned stocks
            # panel shows the failure rather than silently dropping the row.
            if on_scan_result is not None:
                try:
                    on_scan_result({
                        "decision": "error",
                        "error": f"detect_one raised: {exc}",
                        "tool_calls": [],
                        "agent_iters": 0,
                    })
                except Exception:  # noqa: BLE001
                    log.debug("llm_breakout: on_scan_result raised", exc_info=True)
            return []

        # The detector ALWAYS returns an AgentResult now. Forward it to the
        # lead generator so it can persist a LeadScanOutcome row regardless
        # of whether the agent emitted any signals.
        if on_scan_result is not None:
            try:
                # Mirror the AgentResult shape into a lead-generator-friendly
                # dict. Keep tool_calls / agent_iters / duration for the UI.
                on_scan_result({
                    "decision": (
                        "generated" if agent_result.signals
                        else ("error" if agent_result.error else "no_signal")
                    ),
                    "short_reason": agent_result.short_reason,
                    "rejection_reason": agent_result.rejection_reason,
                    "rationale": agent_result.rationale,
                    "tool_calls": list(agent_result.tool_calls),
                    "agent_iters": agent_result.agent_iters,
                    "duration_ms": int(agent_result.agent_duration_s * 1000),
                    "error": agent_result.error,
                })
            except Exception:  # noqa: BLE001
                # A bad UI hook must never break the lead-generation run.
                log.debug("llm_breakout: on_scan_result raised", exc_info=True)

        if not agent_result.signals:
            return []

        leads: list[LeadCandidate] = []
        for sig in agent_result.signals:
            meta = {
                "source": "llm",
                "indicators": indicators_for_logging(candles),
                "llm_rationale": str(sig.get("rationale", "")),
                "llm_tool_calls": list(agent_result.tool_calls),
                "llm_agent_iters": agent_result.agent_iters,
                "llm_agent_duration_s": agent_result.agent_duration_s,
                "llm_rejection_reason": agent_result.rejection_reason,
            }
            leads.append(
                LeadCandidate(
                    instrument_id=getattr(instrument, "id", 0),
                    underlying_key=getattr(instrument, "spot_instrument_key", ""),
                    direction=str(sig["direction"]),
                    signal_type=str(sig["pattern_type"]),
                    signal_level=float(sig["trigger_price"]),
                    confidence=float(sig["confidence"]),
                    chart_interval=self.required_interval,
                    meta=meta,
                )
            )
        return leads


__all__ = ["LLMBreakoutStrategy", "LLMClient", "OpenAICompatClient", "StubClient"]


class StubClient:  # noqa: D401  -- test helper, not part of the production API
    """In-memory LLM client. `responses` is consumed in order; one per call.

    Used by tests + the dry-run script. Set `raise_on_call = N` to have the
    next N calls return `{"_transport_error": True}` instead of the
    queued responses (or raise if `errors` are also queued).

    Supports BOTH the legacy `chat_json` interface AND the new
    `chat_with_tools` interface (used by the agent loop). For backwards
    compat with existing test fixtures, a queued response that has no
    `tool_calls` field is emitted as a chat_json-style final answer when
    `chat_with_tools` is called.
    """

    def __init__(self, responses=None, errors=None):
        from typing import Any
        self._responses = list(responses or [])
        self._errors = list(errors or [])
        # Keep `calls` shape compatible with the legacy interface: each
        # entry is `(system, user)` for the single-turn path.
        self.calls: list[Any] = []
        self.tool_calls: list[Any] = []
        self.raise_on_call = 0

    def chat_json(self, system: str, user: str):  # noqa: D401
        self.calls.append((system, user))
        if self.raise_on_call > 0:
            self.raise_on_call -= 1
            return {"_transport_error": True}
        if self._errors:
            raise self._errors.pop(0)
        if not self._responses:
            return {"signals": []}
        return self._responses.pop(0)

    def chat_with_tools(self, system, messages, tools=None):
        # Track the call for legacy tests — use the *user* message string so
        # the existing `sys_msg, user_msg = client.calls[0]` unpacking still
        # works.
        user_msg = ""
        for m in (messages or []):
            if isinstance(m, dict) and m.get("role") == "user":
                user_msg = str(m.get("content") or "")
                break
        self.calls.append((system, user_msg))
        if self.raise_on_call > 0:
            self.raise_on_call -= 1
            return None
        if self._errors:
            raise self._errors.pop(0)
        if not self._responses:
            return {"role": "assistant", "content": "{\"signals\": []}"}
        resp = self._responses.pop(0)
        # Legacy chat_json-style response (dict with `signals` / no
        # `tool_calls` / no `content`) — wrap as a final assistant message
        # so the agent loop emits it without trying to call tools.
        if isinstance(resp, dict) and "tool_calls" not in resp and "content" not in resp:
            import json
            return {"role": "assistant", "content": json.dumps(resp)}
        return resp
