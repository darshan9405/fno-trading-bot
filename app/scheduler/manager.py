"""APScheduler wiring for the three jobs (single backend process)."""

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.scheduler.lead_generator import run_lead_generator
from app.scheduler.order_placer import run_order_placer
from app.scheduler.trade_tracker import run_trade_tracker

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def init_scheduler() -> BackgroundScheduler:
    """Idempotent: start the three jobs once. Run in the single gunicorn worker."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    scheduler = BackgroundScheduler(timezone="Asia/Kolkata", daemon=True)
    scheduler.add_job(run_lead_generator, "interval", minutes=5, id="lead_generator",
                      max_instances=1, coalesce=True)
    scheduler.add_job(run_trade_tracker, "interval", seconds=30, id="trade_tracker",
                      max_instances=1, coalesce=True)
    scheduler.add_job(run_order_placer, "interval", seconds=30, id="order_placer",
                      max_instances=1, coalesce=True)
    scheduler.start()
    _scheduler = scheduler
    log.info("schedulers started: lead_generator/15m, trade_tracker/30s, order_placer/30s")
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None