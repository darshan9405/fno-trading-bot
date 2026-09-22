"""LLM-driven breakout strategy.

Replaces the math-based detectors (`app.strategy.breakout.*`, now removed)
with a single OpenAI-compatible chat call (default: OpenRouter) per
instrument. The LLM decides which pattern (if any) is breaking out, the
trigger price, the confidence, and whether the volume confirmation passes.
The app handles format conversion, structural validation, and persistence.

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
  - LLM_API_KEY            OpenRouter key (default provider)
  - LLM_BASE_URL           defaults to https://openrouter.ai/api/v1
  - LLM_MODEL              OpenRouter slug, e.g. "minimax/minimax-m3"
  - LLM_TIMEOUT_S          request timeout (seconds)
  - LLM_MAX_RETRIES        transport retries on 429/5xx
  - OPENROUTER_APP_URL     optional -> HTTP-Referer header
  - OPENROUTER_APP_NAME    optional -> X-Title header

Model slugs use the OpenRouter `provider/model` form, e.g.:
  - minimax/minimax-m3           (default)
  - anthropic/claude-3.5-sonnet
  - openai/gpt-4o
  - google/gemini-pro-1.5
  - meta-llama/llama-3.1-70b-instruct

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

    def generate(self, instrument, candles: pd.DataFrame, now) -> list[LeadCandidate]:
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
            signals = detect_one(
                client=client,
                symbol=getattr(instrument, "symbol", ""),
                underlying_key=getattr(instrument, "spot_instrument_key", ""),
                candles=candles,
                lookback_candles=lookback,
                divergence_pct=divergence_pct,
                min_confidence=min_conf,
            )
        except Exception as exc:  # noqa: BLE001 — never let an LLM error crash the tick
            log.exception("llm_breakout: detect_one raised for %s: %s",
                          getattr(instrument, "symbol", "?"), exc)
            return []

        if not signals:
            return []

        leads: list[LeadCandidate] = []
        for sig in signals:
            meta = {
                "source": "llm",
                "indicators": indicators_for_logging(candles),
                "llm_rationale": str(sig.get("rationale", "")),
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
    """

    def __init__(self, responses=None, errors=None):
        from typing import Any
        self._responses = list(responses or [])
        self._errors = list(errors or [])
        self.calls = []
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
