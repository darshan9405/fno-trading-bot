"""APScheduler wiring for the four jobs (single backend process)."""

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.scheduler.lead_cleanup import run_lead_cleanup
from app.scheduler.lead_generator import run_lead_generator
from app.scheduler.order_placer import run_order_placer
from app.scheduler.reconciler import run_reconciler
from app.scheduler.trade_tracker import run_trade_tracker
from app.settings import get_setting

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None

DEFAULT_LEAD_GENERATOR_SECONDS = 300
DEFAULT_TRADE_TRACKER_SECONDS = 30
DEFAULT_ORDER_PLACER_SECONDS = 30
DEFAULT_LEAD_CLEANUP_SECONDS = 30
DEFAULT_RECONCILER_SECONDS = 60


def init_scheduler() -> BackgroundScheduler:
    """Idempotent: start the four jobs once. Run in the single gunicorn worker.

    Intervals are read from the `settings` table (keys
    `scheduler.lead_generator_seconds`, `scheduler.trade_tracker_seconds`,
    `scheduler.order_placer_seconds`, `scheduler.lead_cleanup_seconds`,
    `scheduler.reconciler_seconds`); falls back to module defaults if unset.
    """
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    lead_seconds = int(get_setting("scheduler.lead_generator_seconds", DEFAULT_LEAD_GENERATOR_SECONDS))
    track_seconds = int(get_setting("scheduler.trade_tracker_seconds", DEFAULT_TRADE_TRACKER_SECONDS))
    place_seconds = int(get_setting("scheduler.order_placer_seconds", DEFAULT_ORDER_PLACER_SECONDS))
    cleanup_seconds = int(get_setting("scheduler.lead_cleanup_seconds", DEFAULT_LEAD_CLEANUP_SECONDS))
    reconciler_seconds = int(get_setting("scheduler.reconciler_seconds", DEFAULT_RECONCILER_SECONDS))

    scheduler = BackgroundScheduler(timezone="Asia/Kolkata", daemon=True)
    scheduler.add_job(run_lead_generator, "interval", seconds=lead_seconds, id="lead_generator",
                      max_instances=1, coalesce=True)
    scheduler.add_job(run_trade_tracker, "interval", seconds=track_seconds, id="trade_tracker",
                      max_instances=1, coalesce=True)
    scheduler.add_job(run_order_placer, "interval", seconds=place_seconds, id="order_placer",
                      max_instances=1, coalesce=True)
    scheduler.add_job(run_lead_cleanup, "interval", seconds=cleanup_seconds, id="lead_cleanup",
                      max_instances=1, coalesce=True)
    scheduler.add_job(run_reconciler, "interval", seconds=reconciler_seconds, id="reconciler",
                      max_instances=1, coalesce=True)
    # Drift cleanup piggy-backs on the lead-cleanup interval. Runs in the same
    # tick so it doesn't add a 6th scheduler job — drift rows are trimmed in
    # the same DB transaction window. See app.services.drift_service.
    from app.services.drift_service import cleanup_old_drifts
    scheduler.add_job(cleanup_old_drifts, "interval", seconds=cleanup_seconds, id="drift_cleanup",
                      max_instances=1, coalesce=True)
    scheduler.start()
    _scheduler = scheduler
    log.info(
        "schedulers started: lead_generator/%ds, trade_tracker/%ds, order_placer/%ds, "
        "lead_cleanup/%ds, reconciler/%ds, drift_cleanup/%ds",
        lead_seconds, track_seconds, place_seconds, cleanup_seconds,
        reconciler_seconds, cleanup_seconds,
    )
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None