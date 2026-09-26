"""Small helpers shared across tools.

Currently a single function: extract OI/IV from an InstrumentView. The
broker SDK returns OI in paise sometimes and as floats other times;
we coerce defensively and fall back to None so the JSON-serialised tool
result never breaks on weird inputs.
"""

from __future__ import annotations

from typing import Any


def chain_oi_iv(contract) -> tuple[float | None, float | None]:
    """Best-effort `(open_interest, iv)` extraction from an InstrumentView.

    Tries attribute names the SDK has historically exposed (`oi`,
    `open_interest`, `iv`, `implied_volatility`). Returns (None, None)
    if neither is present so callers can safely JSON-serialise the result.
    """
    oi = None
    iv = None
    for attr in ("open_interest", "oi"):
        if hasattr(contract, attr):
            try:
                oi = float(getattr(contract, attr) or 0) or None
            except (TypeError, ValueError):
                oi = None
            break
    for attr in ("implied_volatility", "iv"):
        if hasattr(contract, attr):
            try:
                iv = float(getattr(contract, attr) or 0) or None
            except (TypeError, ValueError):
                iv = None
            break
    return oi, iv


def safe_attr(obj: Any, *names: str, default=None):
    """Return the first non-None attribute value from a list of names."""
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    return default