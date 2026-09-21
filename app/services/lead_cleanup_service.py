"""Lead retention: deletes stale processed leads and stale queued leads.

Policy (lead retention):
  - Processed leads (placed / skipped / expired / picked) are retained for
    `leads.retention_hours_processed` (default 168h = 7 days) so the UI can
    display history with skip reasons.
  - Queued leads older than `leads.retention_hours_queued` (default 24h) are
    deleted so a stuck run / crashed UI / unreachable broker can't leave
    stale signals sitting around forever.

The deletion is performed in the same transaction as `Trade.lead_id` being
nulled, so SQLite's `PRAGMA foreign_keys = ON` (configured in `app/db.py`)
remains satisfied even though `Trade.lead_id` is unique-keyed.
"""

import logging
from datetime import timedelta

from sqlalchemy import delete, select, update

from app.db import session_scope
from app.models import Lead, Trade
from app.services.health_service import utcnow
from app.settings import get_setting

log = logging.getLogger(__name__)


def cleanup_processed_leads() -> int:
    """Delete processed leads older than `leads.retention_hours_processed`.

    Called by the `lead_cleanup` scheduler. Returns the number of deleted rows.
    """
    retention_hours = int(get_setting("leads.retention_hours_processed", 168))
    cutoff = utcnow() - timedelta(hours=retention_hours)

    with session_scope() as session:
        ids = list(session.execute(
            select(Lead.id).where(
                Lead.status != "queued",
                Lead.created_at < cutoff,
            )
        ).scalars())
        if not ids:
            return 0
        # Detach dependent Trade rows first so the FK on `trades.lead_id`
        # remains valid (Trade rows are audit data — we keep them, just
        # with lead_id -> NULL).
        session.execute(
            update(Trade).where(Trade.lead_id.in_(ids)).values(lead_id=None)
        )
        deleted = session.execute(
            delete(Lead).where(Lead.id.in_(ids))
        ).rowcount
    log.info("lead_cleanup: removed %d processed lead(s) older than %dh", deleted, retention_hours)
    return deleted


def cleanup_expired_queued_leads(retention_hours: int = 24) -> int:
    """Delete queued leads whose `created_at` is older than `retention_hours`.

    Returns the number of deleted rows. Defensive `Trade.lead_id` nulling is
    included for symmetry, though queued leads cannot have a Trade row in
    practice (the placer assigns the trade on the queued -> picked transition).
    """
    cutoff = utcnow() - timedelta(hours=retention_hours)
    with session_scope() as session:
        ids = list(session.execute(
            select(Lead.id).where(
                Lead.status == "queued",
                Lead.created_at < cutoff,
            )
        ).scalars())
        if not ids:
            return 0
        session.execute(
            update(Trade).where(Trade.lead_id.in_(ids)).values(lead_id=None)
        )
        deleted = session.execute(
            delete(Lead).where(Lead.id.in_(ids))
        ).rowcount
    log.info(
        "lead_cleanup: removed %d queued lead(s) older than %dh", deleted, retention_hours
    )
    return deleted
