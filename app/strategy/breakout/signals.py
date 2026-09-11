"""Pattern signals produced by breakout detectors (strategy maps them to leads)."""

from dataclasses import dataclass, field

from app.strategy.scoring import ComponentScores, composite


@dataclass
class PatternSignal:
    direction: str  # CALL | PUT
    signal_type: str
    signal_level: float
    confidence: float
    components: ComponentScores = field(default_factory=ComponentScores)

    def __post_init__(self) -> None:
        # If a detector emitted substantive components (pattern_fit, proximity,
        # structure, or any extras), re-derive `confidence` from the composite
        # pipeline. Default ComponentScores (all zeros in the substantive
        # fields) are treated as "no evidence" and the caller's `confidence`
        # value is preserved verbatim — this keeps back-compat for synthetic
        # tests and legacy callers that don't yet emit components.
        substantive = (
            self.components.pattern_fit != 0.0
            or self.components.proximity != 0.0
            or self.components.structure != 0.0
            or bool(self.components.extras)
        )
        if substantive:
            try:
                self.confidence = composite(self.components)
            except Exception:
                pass
