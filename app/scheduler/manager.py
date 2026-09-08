"""APScheduler wiring for the three jobs (single backend process)."""

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.scheduler.lead_generator import run_lead_generator
from app.scheduler.order_placer import run_order_placer
from app.scheduler.trade_tracker import run_trade_tracker
from app.settings import get_setting

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None

DEFAULT_LEAD_GENERATOR_SECONDS = 300
DEFAULT_TRADE_TRACKER_SECONDS = 30
DEFAULT_ORDER_PLACER_SECONDS = 30


def init_scheduler() -> BackgroundScheduler:
    """Idempotent: start the three jobs once. Run in the single gunicorn worker.

    Intervals are read from the `settings` table (keys
    `scheduler.lead_generator_seconds`, `scheduler.trade_tracker_seconds`,
    `scheduler.order_placer_seconds`); falls back to module defaults if unset.
    """
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    lead_seconds = int(get_setting("scheduler.lead_generator_seconds", DEFAULT_LEAD_GENERATOR_SECONDS))
    track_seconds = int(get_setting("scheduler.trade_tracker_seconds", DEFAULT_TRADE_TRACKER_SECONDS))
    place_seconds = int(get_setting("scheduler.order_placer_seconds", DEFAULT_ORDER_PLACER_SECONDS))

    scheduler = BackgroundScheduler(timezone="Asia/Kolkata", daemon=True)
    scheduler.add_job(run_lead_generator, "interval", seconds=lead_seconds, id="lead_generator",
                      max_instances=1, coalesce=True)
    scheduler.add_job(run_trade_tracker, "interval", seconds=track_seconds, id="trade_tracker",
                      max_instances=1, coalesce=True)
    scheduler.add_job(run_order_placer, "interval", seconds=place_seconds, id="order_placer",
                      max_instances=1, coalesce=True)
    scheduler.start()
    _scheduler = scheduler
    log.info(
        "schedulers started: lead_generator/%ds, trade_tracker/%ds, order_placer/%ds",
        lead_seconds, track_seconds, place_seconds,
    )
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None