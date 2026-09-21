"""Strategy abstraction for the lead generator.

Scheduler 1 (lead_generator) depends only on this interface:
StrategyRegistry.get(<name>).generate(instrument, candles, now) -> list[LeadCandidate]

New strategies are added as Strategy subclasses and registered by name; the
schedulers, schema, and UI do not change.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

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
    def generate(self, instrument, candles: pd.DataFrame, now) -> list[LeadCandidate]:
        """Analyse `candles` (OHLCV at `required_interval`) for one instrument
        and return candidate leads. `instrument` is an app.models.Instrument.
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