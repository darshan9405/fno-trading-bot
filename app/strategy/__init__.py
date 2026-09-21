"""Strategy package: registers built-in strategies on import."""

from app.strategy.base import (
    LeadCandidate,
    Strategy,
    StrategyRegistry,
    register_strategy,
)

import app.strategy.llm_breakout  # noqa: F401  (register built-in strategy)

__all__ = ["LeadCandidate", "Strategy", "StrategyRegistry", "register_strategy"]