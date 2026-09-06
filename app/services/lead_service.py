"""Lead persistence: candidates -> queued leads, dedup (1 per instrument/day)."""

from datetime import datetime, time

from sqlalchemy import select

from app.db import session_scope
from app.models import Instrument, Lead
from app.services.health_service import utcnow


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