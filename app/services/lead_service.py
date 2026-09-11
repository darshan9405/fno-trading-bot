"""Lead persistence: candidates -> queued leads.

Deduplication is intentionally NOT done here: every candidate is persisted so a
later, better signal (or a re-check after a skipped lead) is never dropped.
Duplicate handling for the same option is left to the order placer.
"""

import logging
from datetime import date

from sqlalchemy import select

from app.db import session_scope
from app.models import Instrument, Lead
from app.services import contract_service
from app.services.health_service import utcnow

log = logging.getLogger(__name__)


def create_leads_from_candidates(session, instrument: Instrument, candidates, now, strategy: str) -> list[Lead]:
    """Insert candidate leads for one instrument.

    Always persists candidates. Dedup against an already-queued/picked/placed
    lead for the same option is handled at order-placement time, so generation
    never suppresses a lead.
    """
    if not candidates:
        return []

    rows = []
    for c in candidates:
        components = (c.meta or {}).get("components") if getattr(c, "meta", None) else None
        rows.append(Lead(
            instrument_id=c.instrument_id,
            underlying_key=c.underlying_key,
            direction=c.direction,
            strategy=strategy,
            signal_type=c.signal_type,
            signal_level=c.signal_level,
            confidence=c.confidence,
            chart_interval=c.chart_interval,
            status="queued",
            components=components,
        ))
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
    max_depth: int = 3,
) -> None:
    """Resolve and persist the F&O contract each lead would trade.

    Groups broker calls by underlying (expiries + option contracts + live spot
    are fetched once per underlying). The contract is chosen margin-aware: start
    at the ATM strike (strike nearest the current price) and walk toward cheaper
    OTM (PUT down, CALL up) up to `max_depth` steps, picking the first strike
    whose premium x lot size x lots fits `available_margin`. A lead is skipped
    (note "insufficient margin") when nothing within depth is affordable.

    If `available_margin` is None the check is disabled and the ATM contract is
    used. Failures are logged and skipped so lead creation is never blocked.
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

        spot = getattr(group[0], "signal_level", None)
        try:
            spot = (broker.get_ltp([underlying_key]) or {}).get(underlying_key) or spot
        except Exception as e:
            log.warning("attach_lead_plans: live spot for %s unavailable (%s)", underlying_key, e)

        for lead in group:
            wanted = "CE" if lead.direction == "CALL" else "PE"
            matches = [c for c in contracts if c.instrument_type == wanted]
            if not matches:
                continue
            try:
                candidates = list(contract_service.walk_candidates(matches, lead.direction, spot, max_depth))
                premiums = broker.get_ltp([c.instrument_key for c in candidates]) or {}
            except Exception as e:
                log.warning("attach_lead_plans: premiums for %s unavailable (%s)", underlying_key, e)
                premiums = {}
            chosen, cheapest_cost, evaluated = contract_service.select_affordable(
                candidates, premiums, available_margin, lots
            )
            if chosen is None:
                if evaluated and cheapest_cost is not None:
                    mark_lead(
                        session, lead, "skipped",
                        note=f"insufficient margin: need ≥ ₹{cheapest_cost:,.0f}, available ₹{available_margin:,.0f}",
                    )
                continue
            lead.plan = {
                "expiry": expiry.isoformat(),
                "strike_price": chosen.strike_price,
                "option_type": chosen.instrument_type,
                "trading_symbol": chosen.trading_symbol,
                "lot_size": chosen.lot_size,
                "quantity": chosen.lot_size * lots,
                "spot": spot,
                "premium": premiums.get(chosen.instrument_key),
            }
            if lead.plan["premium"] is not None:
                lead.plan["margin_needed"] = round(lead.plan["premium"] * lead.plan["quantity"], 2)