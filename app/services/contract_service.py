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


def option_tick_for_instrument(broker, instrument_key: str) -> float:
    """Resolve the option tick for a specific instrument via the broker.

    Used by square-off / trailing paths to make sure we snap the SL limit
    to the correct tick band (0.05/0.10/0.50). Returns 0.0 if the broker
    can't be queried or the instrument isn't found — callers should fall
    back to the FNO_OPTION_TICK default in that case.
    """
    try:
        instruments = broker.get_instruments()
    except Exception:
        return 0.0
    for inst in instruments or []:
        key = getattr(inst, "instrument_key", None) or getattr(inst, "instrument_token", None)
        if key is not None and str(key) == str(instrument_key):
            for attr in ("tick_size", "minimum_price_increment"):
                if hasattr(inst, attr):
                    try:
                        return float(getattr(inst, attr) or 0.0) or 0.0
                    except (TypeError, ValueError):
                        return 0.0
            break
    return 0.0


def option_chain_summary(broker, contracts, spot: float, *, depth: int = 3) -> dict:
    """Compact at-the-money option-chain snapshot for the LLM context tool.

    Returns a JSON-safe dict summarising the chain `depth` strikes either
    side of ATM:
      - atm_strike            nearest strike to `spot`
      - atm_ce / atm_pe       premiums at the ATM strike (None if no LTP)
      - atm_straddle          atm_ce + atm_pe (None if either missing)
      - atm_straddle_pct      straddle as a % of spot
      - total_ce_oi / total_pe_oi    sum of open interest in the window
      - pcr_oi                total_pe_oi / total_ce_oi (None if ce==0)
      - max_pain_strike       strike with the highest total OI (CE+PE)
      - iv_skew_proxy         atm_ce - atm_pe (None if missing) — proxy for
                              skew; positive means calls are richer (bullish
                              premium).
      - strikes_window        sorted list of strikes included
    """
    from app.strategy.llm_breakout.tools.helpers import chain_oi_iv

    wanted_strikes: list[float] = []
    seen = set()
    for c in contracts:
        if c.strike_price is None or c.instrument_type not in ("CE", "PE"):
            continue
        if c.strike_price in seen:
            continue
        seen.add(c.strike_price)
        wanted_strikes.append(c.strike_price)
    if not wanted_strikes:
        return {"error": "no strikes found"}
    wanted_strikes.sort()
    atm_idx = min(range(len(wanted_strikes)), key=lambda i: abs(wanted_strikes[i] - spot))
    lo = max(0, atm_idx - depth)
    hi = min(len(wanted_strikes), atm_idx + depth + 1)
    window = wanted_strikes[lo:hi]
    atm_strike = wanted_strikes[atm_idx]

    # Fetch LTP for CE/PE at each window strike + OI from broker instruments.
    by_strike_type: dict[tuple[float, str], object] = {}
    for c in contracts:
        if c.strike_price in window and c.instrument_type in ("CE", "PE"):
            by_strike_type[(c.strike_price, c.instrument_type)] = c

    keys = [c.instrument_key for c in by_strike_type.values()]
    ltps = broker.get_ltp(keys) if keys else {}

    chain_view: list[dict] = []
    total_ce_oi = 0.0
    total_pe_oi = 0.0
    for strike in window:
        ce = by_strike_type.get((strike, "CE"))
        pe = by_strike_type.get((strike, "PE"))
        row: dict = {"strike": float(strike)}
        if ce is not None:
            row["ce_ltp"] = ltps.get(ce.instrument_key)
            oi_ce, _ = chain_oi_iv(ce)
            row["ce_oi"] = oi_ce
            total_ce_oi += float(oi_ce or 0)
        if pe is not None:
            row["pe_ltp"] = ltps.get(pe.instrument_key)
            oi_pe, _ = chain_oi_iv(pe)
            row["pe_oi"] = oi_pe
            total_pe_oi += float(oi_pe or 0)
        chain_view.append(row)

    atm_ce = next((r for r in chain_view if abs(r["strike"] - atm_strike) < 1e-9 and r.get("ce_ltp") is not None), None)
    atm_pe = next((r for r in chain_view if abs(r["strike"] - atm_strike) < 1e-9 and r.get("pe_ltp") is not None), None)
    atm_ce_ltp = atm_ce["ce_ltp"] if atm_ce else None
    atm_pe_ltp = atm_pe["pe_ltp"] if atm_pe else None
    straddle = (
        atm_ce_ltp + atm_pe_ltp
        if (atm_ce_ltp is not None and atm_pe_ltp is not None)
        else None
    )
    straddle_pct = (straddle / spot * 100.0) if (straddle is not None and spot > 0) else None

    # Max-pain strike (sum of CE+PE OI per strike).
    max_pain = None
    max_pain_oi = None
    for row in chain_view:
        s_oi = (row.get("ce_oi") or 0) + (row.get("pe_oi") or 0)
        if max_pain_oi is None or s_oi > max_pain_oi:
            max_pain_oi = s_oi
            max_pain = row["strike"]

    pcr_oi = (total_pe_oi / total_ce_oi) if total_ce_oi > 0 else None
    iv_skew = (
        atm_ce_ltp - atm_pe_ltp
        if (atm_ce_ltp is not None and atm_pe_ltp is not None)
        else None
    )
    return {
        "atm_strike": float(atm_strike),
        "spot": float(spot),
        "atm_ce_ltp": atm_ce_ltp,
        "atm_pe_ltp": atm_pe_ltp,
        "atm_straddle": straddle,
        "atm_straddle_pct": straddle_pct,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "pcr_oi": pcr_oi,
        "max_pain_strike": max_pain,
        "iv_skew_proxy": iv_skew,
        "strikes_window": [float(s) for s in window],
        "chain": chain_view,
    }