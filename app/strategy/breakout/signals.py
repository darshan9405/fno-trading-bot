"""Pattern signals produced by breakout detectors (strategy maps them to leads)."""

from dataclasses import dataclass


@dataclass
class PatternSignal:
    direction: str  # CALL | PUT
    signal_type: str
    signal_level: float
    confidence: float