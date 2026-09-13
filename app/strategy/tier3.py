"""Tier-3 context features (IV / OI / time-of-day).

Optional add-ons layered on top of the composite score. Each factor is fully
defensive: if its data source is unavailable (off-hours, broker error, missing
table), the original `ComponentScores` is returned unchanged so the pipeline
gracefully degrades.

Enable via:
    scoring.enable_iv           = True/False
    scoring.enable_oi           = True/False
    scoring.enable_time_of_day  = True/False

The features contribute via `app.strategy.scoring.available_weight_map()`
which shrinks the Tier-1 base weights to keep the total at 1.0.

NOTE on IV/OI wiring: detectors in `app.strategy.breakout` work off daily
candles. The Tier-3 IV/OI scoring happens at order-placement time instead,
where the broker call is already happening for strike selection — see
`attach_context_for_lead` (called by `order_placer`).
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.strategy.scoring import ComponentScores

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


# --- Time-of-day -----------------------------------------------------------

# Bias windows: NSE intraday momentum historically clusters between 10:00-11:30
# (opening range) and 13:00-14:00 (positional repricing). Mid-day is the
# dead zone; 09:15-10:00 is opening noise.
def time_of_day_score(now: datetime | None) -> float:
    """Score in [0, 1] reflecting the historical hit-rate distribution
    within the NSE trading session. Returns 0.5 (neutral) outside the
    session so we never zero out a signal just because it fired after hours.
    """
    if now is None:
        return 0.5
    try:
        local = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    except Exception:
        return 0.5
    minutes = local.hour * 60 + local.minute
    # Outside trading hours: neutral.
    if minutes < 9 * 60 + 15 or minutes >= 15 * 60 + 30:
        return 0.5
    # 09:15-10:00: low (just opened, lots of noise).
    if minutes < 10 * 60:
        return 0.55
    # 10:00-11:30: strong (opening drive).
    if minutes < 11 * 60 + 30:
        return 0.9
    # 11:30-13:00: weaker (lunch lull).
    if minutes < 13 * 60:
        return 0.6
    # 13:00-14:30: strong again (positional reprice).
    if minutes < 14 * 60 + 30:
        return 0.85
    # 14:30-15:30: weak (last-hour noise).
    return 0.55


# --- Public entry point ----------------------------------------------------

def attach_market_context(comps: ComponentScores, instrument, now) -> ComponentScores:
    """Apply available Tier-3 context at lead-generation time. Defensive —
    any setting/db/broker failure is logged at DEBUG and the original
    `ComponentScores` is returned unchanged.

    A Tier-3 feature is only added to `extras` when it has actual data:
      - time_of_day: requires a non-None `now` (we know the IST clock).
      - iv / oi: NOT added here. They need the option chain, which is
        fetched later in `attach_lead_plans` once the strike is resolved.
        `enrich_for_order_placement` / `reblend_with_tier3` add them then.

    Adding a neutral default (0.5) at generate time would silently penalise
    otherwise-good signals by ~Tier-3 weight × 0.5 — see the score math in
    the docstring of `app.strategy.scoring.available_weight_map`.
    """
    try:
        from app.settings import get_setting
        enable_tod = bool(get_setting("scoring.enable_time_of_day", False))
    except Exception:
        enable_tod = False

    extras: dict[str, float] = {}
    if enable_tod and now is not None:
        extras["time_of_day"] = time_of_day_score(now)

    if not extras:
        return comps
    return comps.with_extra(**extras)


# --- Pipe-through (no-op here; IV/OI happens in tier3_context service) ----

def enrich_for_order_placement(comps: ComponentScores, broker, underlying_key: str,
                                direction: str, signal_level: float,
                                contracts=None) -> ComponentScores:
    """Append IV/OI extras when their settings flag is on. Called from
    `attach_lead_plans` (after strike is resolved — the option chain is
    already in hand) or `order_placer`. If `contracts` is supplied, the
    function skips re-fetching the chain. Defensive — returns `comps`
    unchanged on any error.
    """
    try:
        from app.settings import get_setting
        enable_iv = bool(get_setting("scoring.enable_iv", False))
        enable_oi = bool(get_setting("scoring.enable_oi", False))
    except Exception:
        enable_iv = enable_oi = False

    if not (enable_iv or enable_oi):
        return comps

    if contracts is None:
        try:
            contracts = _fetch_option_chain(broker, underlying_key)
        except Exception as e:
            log.debug("tier3: option-chain fetch failed for %s (%s)", underlying_key, e)
            return comps

    extras: dict[str, float] = {}
    if enable_iv:
        extras["iv"] = _iv_score(contracts, signal_level)
    if enable_oi:
        extras["oi"] = _oi_score(contracts, direction, signal_level)
    if not extras:
        return comps
    return comps.with_extra(**extras)


def reblend_with_tier3(comps: ComponentScores, underlying_key: str,
                       direction: str, signal_level: float, contracts=None,
                       broker=None) -> tuple[ComponentScores, float]:
    """Re-blend the composite score including all enabled Tier-3 features
    (IV / OI / time-of-day). Used by `attach_lead_plans` after the strike
    has been resolved so IV/OI can be computed against the actual contract.

    Returns (updated ComponentScores, new score in [0, 1]). Defensive —
    on any error returns the input `comps` and its current score.
    """
    try:
        from app.settings import get_setting
        enable_iv = bool(get_setting("scoring.enable_iv", False))
        enable_oi = bool(get_setting("scoring.enable_oi", False))
        enable_tod = bool(get_setting("scoring.enable_time_of_day", False))
    except Exception:
        return comps, _approx_score(comps)

    enriched = enrich_for_order_placement(
        comps, broker, underlying_key, direction, signal_level, contracts=contracts,
    )

    try:
        from app.strategy.scoring import available_weight_map, composite
        weights = available_weight_map(
            enable_iv=enable_iv, enable_oi=enable_oi, enable_tod=enable_tod,
        )
        new_score = composite(enriched, weights)
        return enriched, new_score
    except Exception:
        return comps, _approx_score(comps)


def _approx_score(comps: ComponentScores) -> float:
    """Best-effort current score for fallback when the weight map isn't
    available. Falls back to a simple average of populated fields."""
    vals = [comps.pattern_fit, comps.volume, comps.trend_alignment,
            comps.proximity, comps.structure]
    vals += list(comps.extras.values())
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def _fetch_option_chain(broker, underlying_key: str):
    """Best-effort option-chain fetch for IV/OI scoring."""
    from app.services.contract_service import next_expiry
    expiry = next_expiry(broker, underlying_key, min_days=0)
    if expiry is None:
        return None
    return broker.get_option_contracts(underlying_key, expiry=expiry) or []


def _iv_score(contracts, signal_level: float) -> float:
    """Default neutral-to-positive (0.7) if we can't compute a sensible number.
    High IV slightly discounts the lead; mid IV neutral; low/compressed IV
    boosts the lead (breakout into compression is more meaningful)."""
    if not contracts:
        return 0.7
    try:
        atm = min(contracts, key=lambda c: abs(getattr(c, "strike_price", 0) - signal_level))
        iv = getattr(atm, "iv", None)
        if iv is None:
            return 0.7
        if iv <= 0.20:
            return 0.95
        if iv <= 0.40:
            return 0.7
        return 0.4
    except Exception:
        return 0.7


def _oi_score(contracts, direction: str, signal_level: float) -> float:
    """Boost signals that align with dominant OI build-up nearby."""
    if not contracts:
        return 0.7
    try:
        band = max(1.0, signal_level * 0.01)
        nearby = [c for c in contracts if abs(getattr(c, "strike_price", 0) - signal_level) <= band]
        if not nearby:
            return 0.7
        call_oi = sum(getattr(c, "oi", 0) or 0 for c in nearby if getattr(c, "instrument_type", "") == "CE")
        put_oi = sum(getattr(c, "oi", 0) or 0 for c in nearby if getattr(c, "instrument_type", "") == "PE")
        if call_oi == 0 and put_oi == 0:
            return 0.7
        if call_oi > put_oi:
            return 0.95 if direction == "CALL" else 0.4
        if put_oi > call_oi:
            return 0.95 if direction == "PUT" else 0.4
        return 0.7
    except Exception:
        return 0.7
