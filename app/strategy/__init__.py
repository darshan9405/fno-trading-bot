"""Strategy package: registers built-in strategies on import."""

from app.strategy.base import (
    LeadCandidate,
    Strategy,
    StrategyRegistry,
    register_strategy,
)

import app.strategy.breakout  # noqa: F401  (register built-in strategies)

__all__ = ["LeadCandidate", "Strategy", "StrategyRegistry", "register_strategy"]