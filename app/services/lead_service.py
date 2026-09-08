"""Lead persistence: candidates -> queued leads, dedup (1 per instrument/day)."""

import logging
from datetime import date, datetime, time

from sqlalchemy import select

from app.db import session_scope
from app.models import Instrument, Lead
from app.services import contract_service
from app.services.health_service import utcnow

log = logging.getLogger(__name__)


def create_leads_from_candidates(session, instrument: Instrument, candidates, now, strategy: str) -> list[Lead]:
    """Insert candidate leads for one instrument, deduped to one batch per day."""
    if not candidates:
        return []
    day_start = datetime.combine(utcnow().date(), time.min)
    existing = session.execute(
        select(Lead).where(Lead.instrument_id == instrument.id, Lead.created_at >= day_start)
    ).scalars().first()
    if existing:
        return []

    rows = [
        Lead(
            instrument_id=c.instrument_id,
            underlying_key=c.underlying_key,
            direction=c.direction,
            strategy=strategy,
            signal_type=c.signal_type,
            signal_level=c.signal_level,
            confidence=c.confidence,
            chart_interval=c.chart_interval,
            status="queued",
        )
        for c in candidates
    ]
    session.add_all(rows)
    return rows


def get_queued_leads() -> list[Lead]:
    with session_scope() as session:
        return list(session.execute(select(Lead).where(Lead.status == "queued").order_by(Lead.created_at)).scalars())


def get_lead(lead_id: int) -> Lead | None:
    with session_scope() as session:
        return session.get(Lead, lead_id)


def mark_lead(session, lead: Lead, status: str, note: str | None = None) -> None:
    lead.status = status
    if note:
        lead.note = note
    lead.processed_at = utcnow()


def attach_lead_plans(
    session,
    broker,
    leads: list[Lead],
    min_days: int,
    lots: int,
    today: date | None = None,
    available_margin: float | None = None,
    strikes_below: int = 3,
) -> None:
    """Resolve and persist the F&O contract each lead would trade (informational).

    Groups broker calls by underlying (expiries + option contracts are fetched
    once per underlying); the ATM strike is picked against the lead's own
    signal_level. Failures are logged and skipped so lead creation is never
    blocked — the order placer re-resolves at placement time anyway.

    Margin affordability: a lead is skipped (note "insufficient margin") when the
    estimated entry cost exceeds `available_margin`. The estimate uses the deepest
    in-the-money strike within `strikes_below` of the ATM contract — the most
    expensive option the trader would consider — priced at its live premium.
    For a CALL that is `strikes_below` below ATM; for a PUT it is `strikes_below`
    above (symmetric ITM). If `available_margin` is None the check is skipped.
    """
    if available_margin is None:
        try:
            available_margin = getattr(broker.get_funds(), "available_margin", None)
        except Exception as e:
            log.warning("attach_lead_plans: margin unavailable (%s); margin check skipped", e)
            available_margin = None

    by_underlying: dict[str, list[Lead]] = {}
    for lead in leads:
        if lead.direction in ("CALL", "PUT"):
            by_underlying.setdefault(lead.underlying_key, []).append(lead)

    for underlying_key, group in by_underlying.items():
        try:
            expiry = contract_service.next_expiry(broker, underlying_key, min_days, today)
            if expiry is None:
                continue
            contracts = broker.get_option_contracts(underlying_key, expiry=expiry)
        except Exception as e:
            log.warning("attach_lead_plans: skip %s (%s)", underlying_key, e)
            continue

        for lead in group:
            wanted = "CE" if lead.direction == "CALL" else "PE"
            matches = [c for c in contracts if c.instrument_type == wanted]
            if not matches:
                continue
            try:
                contract = min(matches, key=lambda c: abs(c.strike_price - lead.signal_level))
                lead.plan = {
                    "expiry": expiry.isoformat(),
                    "strike_price": contract.strike_price,
                    "option_type": contract.instrument_type,
                    "trading_symbol": contract.trading_symbol,
                    "lot_size": contract.lot_size,
                    "quantity": contract.lot_size * lots,
                }
                if available_margin is not None:
                    _check_margin(session, broker, lead, matches, contract, lots, available_margin, strikes_below)
            except Exception as e:
                log.warning("attach_lead_plans: lead %s skip (%s)", lead.id, e)


def _deepest_itm_contract(matches, contract, direction: str, strikes_below: int):
    """Deepest in-the-money contract within `strikes_below` of the ATM contract.

    For a CALL that is `strikes_below` strikes below the ATM; for a PUT it is
    `strikes_below` above (the symmetric ITM side). Clamps to the boundary when
    fewer strikes are available.
    """
    by_strike = sorted(matches, key=lambda c: c.strike_price)
    try:
        idx = next(i for i, c in enumerate(by_strike) if c.strike_price == contract.strike_price)
    except StopIteration:
        return None
    if direction == "CALL":
        return by_strike[max(0, idx - strikes_below)]
    return by_strike[min(len(by_strike) - 1, idx + strikes_below)]


def _check_margin(session, broker, lead: Lead, matches, contract, lots: int, available_margin: float, strikes_below: int) -> None:
    """Skip the lead when the deepest-tradable entry cost exceeds available margin."""
    deep = _deepest_itm_contract(matches, contract, lead.direction, strikes_below)
    if deep is None:
        return
    try:
        premium = (broker.get_ltp([deep.instrument_key]) or {}).get(deep.instrument_key)
    except Exception as e:
        log.warning("attach_lead_plans: margin premium for %s unavailable (%s)", deep.instrument_key, e)
        return
    if premium is None:
        return
    need = premium * deep.lot_size * lots
    if need > available_margin:
        mark_lead(
            session, lead, "skipped",
            note=f"insufficient margin: need ₹{need:,.0f}, available ₹{available_margin:,.0f}",
        )