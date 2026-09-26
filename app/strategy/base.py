"""Strategy abstraction for the lead generator.

Scheduler 1 (lead_generator) depends only on this interface:
StrategyRegistry.get(<name>).generate(instrument, candles, now) -> list[LeadCandidate]

New strategies are added as Strategy subclasses and registered by name; the
schedulers, schema, and UI do not change.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd


@dataclass
class LeadCandidate:
    """Normalised signal produced by any strategy."""

    instrument_id: int
    underlying_key: str
    direction: str  # CALL | PUT
    signal_type: str  # strategy-specific label, e.g. "horizontal_range", "gap_fade"
    signal_level: float  # the trigger price that fired the signal
    confidence: float
    chart_interval: str
    meta: dict = field(default_factory=dict)  # arbitrary strategy payload


# A strategy can hand the lead generator extra per-instrument telemetry via
# this callback. The signature is loose (TypedDict would be nicer but
# requires Python 3.12+) — concrete shape is decided per strategy. For
# `llm_breakout` it's an `AgentResult` (see `app.strategy.llm_breakout.agent`).
ScanResultCallback = Callable[["Any"], None]


class Strategy(ABC):
    """Base class for lead-generation strategies."""

    name: str
    required_interval: str = "day"

    def begin_run(self, max_calls: int) -> None:
        """Hook the lead generator calls once before a run, with the run-wide
        call budget read from `llm.max_calls_per_run`. Default is a no-op so
        stateless strategies don't need to override it. Strategies that fan out
        one external call per instrument (e.g. the LLM detector) override this
        to reset their per-run counters, otherwise the cap is silently inert.
        """

    @abstractmethod
    def generate(self, instrument, candles: pd.DataFrame, now, *,
                 on_tool_call: Callable[[dict[str, Any]], None] | None = None,
                 on_scan_result: ScanResultCallback | None = None,
                 **kwargs: Any) -> list[LeadCandidate]:
        """Analyse `candles` (OHLCV at `required_interval`) for one instrument
        and return candidate leads. `instrument` is an app.models.Instrument.

        Two optional hooks are forwarded in by the lead generator:
          - `on_tool_call(event)` fires once per external tool invocation
            inside the strategy (e.g. one LLM tool call per agent iteration).
            Used by the UI to render the live "Analysing X — tool: Y" feed.
          - `on_scan_result(result)` fires once per instrument at the end of
            the per-instrument analysis, with whatever rich payload the
            strategy chooses (LLM AgentResult, indicator snapshot, ...). The
            lead generator persists this into `LeadScanOutcome` so the
            "Scanned stocks" panel can audit why a symbol was rejected.
        Both callbacks are best-effort: a slow / failing callback must not
        break the run. Strategies may pass None when they don't have
        anything to surface.
        """
        raise NotImplementedError


class StrategyRegistry:
    """Name -> Strategy class registry (factory)."""

    _strategies: dict[str, type[Strategy]] = {}

    @classmethod
    def register(cls, name: str | None = None):
        def decorator(strategy_cls: type[Strategy]) -> type[Strategy]:
            key = name or getattr(strategy_cls, "name", None)
            if not key:
                raise ValueError(f"Strategy {strategy_cls.__name__} must define 'name'")
            cls._strategies[key] = strategy_cls
            return strategy_cls

        return decorator

    @classmethod
    def get(cls, name: str) -> type[Strategy]:
        if name not in cls._strategies:
            raise KeyError(f"Unknown strategy: {name!r}. Registered: {sorted(cls._strategies)}")
        return cls._strategies[name]

    @classmethod
    def all(cls) -> dict[str, type[Strategy]]:
        return dict(cls._strategies)


def register_strategy(name: str | None = None):
    return StrategyRegistry.register(name)