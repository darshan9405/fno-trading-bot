"""Option contract resolution shared by lead generation and order placement.

Both the lead generator (to preview the F&O instrument a signal maps to) and
the order placer (to pick the exact contract to trade) need: next available
expiry >= N days and the ATM CE/PE contract at that expiry.
"""

from datetime import date

from app.broker.base import InstrumentView


def next_expiry(broker, underlying_key: str, min_days: int, today: date | None = None) -> date | None:
    """First expiry at least `min_days` from today, or None."""
    today = today or date.today()
    for exp in broker.get_expiries(underlying_key):
        if (exp - today).days >= min_days:
            return exp
    return None


def resolve_option_contract(broker, underlying_key: str, direction: str, expiry: date, spot: float) -> InstrumentView | None:
    """ATM CE (CALL) / PE (PUT) contract for `underlying_key` at `expiry`, or None."""
    wanted = "CE" if direction == "CALL" else "PE"
    matches = [c for c in broker.get_option_contracts(underlying_key, expiry=expiry) if c.instrument_type == wanted]
    if not matches:
        return None
    return min(matches, key=lambda c: abs(c.strike_price - spot))