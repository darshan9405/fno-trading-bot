"""Scheduler 4 — Lead Cleanup.

Deletes lead rows on a short cadence so the Leads UI always reflects the
current actionable set:

  - leads whose status has already left `queued` vanish almost immediately
    (the user does not need to retain processed leads once acted upon);
  - queued leads older than `leads.retention_hours_queued` (default 24h)
    are age-out cleared so a frozen run / unreachable broker cannot leave
    stale signals on screen.

Both cleanup helpers are idempotent and return cheaply when there is nothing
to delete, so this job can run every ~30s without overhead.
"""

import logging

from app.services import health_service, lead_cleanup_service
from app.settings import get_setting

log = logging.getLogger(__name__)

source = "scheduler.lead_cleanup"


def run_lead_cleanup(retention_hours: int | None = None) -> None:
    """Run one cleanup pass. Updates the `lead_cleanup` heartbeat."""
    retention = int(
        retention_hours
        if retention_hours is not None
        else get_setting("leads.retention_hours_queued", 24)
    )

    try:
        n_processed = lead_cleanup_service.cleanup_processed_leads()
        n_expired = lead_cleanup_service.cleanup_expired_queued_leads(retention)
        health_service.touch_heartbeat(
            source,
            f"ok · processed={n_processed} expired_queued={n_expired} retention={retention}h",
        )
    except Exception as e:
        log.exception("lead_cleanup run failed")
        health_service.log_scheduler_error(source, e)
        health_service.touch_heartbeat(source, str(e)[:200], status="error")
