"""Reconciler scheduler: every 60 s, sync DB open-trade status to broker truth.

Catches user UI exits (close at Upstox) and any SL fills the per-trade
trade_tracker hasn't observed yet. Idempotent — closes nothing that's already
closed, ignores trades too fresh to have propagated to get_positions.
"""

import logging

from app.broker import get_broker
from app.config import Config
from app.services import health_service, recon_service
from app.settings import get_setting

log = logging.getLogger(__name__)

source = "scheduler.reconciler"


def run_reconciler(broker=None, now=None):
    now = now or health_service.now_ist()
    try:
        broker = broker or get_broker(Config())
        result = recon_service.reconcile_open_trades(broker)
        reconciled = len(result.get("reconciled", []))
        failed = len(result.get("failed", []))
        skipped = len(result.get("skipped", []))
        note = f"reconciled={reconciled} failed={failed} skipped={skipped}"
        health_service.touch_heartbeat("reconciler", note)
    except Exception as e:
        log.exception("reconciler run failed")
        health_service.log_scheduler_error(source, e)
        health_service.touch_heartbeat("reconciler", str(e)[:200], status="error")
