"""Option contract resolution shared by lead generation and order placement.

Both the lead generator (to preview the F&O instrument a signal maps to) and
the order placer (to pick the exact contract to trade) need: next available
expiry >= N days and a CE/PE contract at that expiry.

Margin-aware strike selection walks from the ATM strike toward cheaper OTM
strikes (PUT walks down, CALL walks up) and picks the first contract whose
entry cost fits the available margin, bounded by a max depth.
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


def walk_candidates(matches, direction: str, spot: float, max_depth: int):
    """Candidate contracts from ATM toward OTM, in priority order.

    Starts at the strike nearest `spot` (ATM); a PUT walks down (lower strikes,
    cheaper), a CALL walks up (higher strikes, cheaper), at most `max_depth`
    steps. Stops early when the strike chain runs out.
    """
    by_strike = sorted(matches, key=lambda c: c.strike_price)
    if not by_strike:
        return
    atm_idx = min(range(len(by_strike)), key=lambda i: abs(by_strike[i].strike_price - spot))
    step = -1 if direction == "PUT" else 1
    for depth in range(max_depth + 1):
        idx = atm_idx + step * depth
        if idx < 0 or idx >= len(by_strike):
            break
        yield by_strike[idx]


def select_affordable(candidates, premiums: dict[str, float], available_margin: float | None, lots: int):
    """Pick the first candidate (ATM -> OTM) whose entry cost fits the margin.

    Returns `(chosen, cheapest_cost, evaluated)`:
    - margin disabled (`available_margin is None`) -> ATM candidate, no skip.
    - margin enabled: first candidate with `premium * lot_size * lots <= margin`,
      else `(None, deepest_cost, True)` for the caller to skip.
    - no candidate had a premium (no live LTP) -> ATM candidate, no skip.
    """
    if not candidates:
        return None, None, False
    if available_margin is None:
        return candidates[0], None, False
    cheapest_cost = None
    evaluated = False
    for c in candidates:
        premium = premiums.get(c.instrument_key)
        if premium is None:
            continue
        evaluated = True
        cost = premium * c.lot_size * lots
        cheapest_cost = cost
        if cost <= available_margin:
            return c, cheapest_cost, True
    if not evaluated:
        return candidates[0], None, False
    return None, cheapest_cost, True