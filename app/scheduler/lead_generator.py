"""Scheduler 1 — Lead Generator.

Runs inside the trading window (weekdays, non-holidays, default 10:00-14:00
IST): fetches candles for each enabled underlying in parallel, runs the
configured strategy via the StrategyRegistry, and persists candidate leads
(deduped to one batch per instrument per day). Strategy `generate()` is empty
until Stage 9 fills in the breakout detectors.
"""

import logging
import random
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

from sqlalchemy import select

from app.broker import get_broker
from app.config import Config
from app.db import session_scope
from app.models import Instrument
from app.services import health_service, instrument_service, market_calendar
from app.services.lead_service import attach_lead_plans, create_leads_from_candidates
from app.settings import get_setting
from app.strategy import StrategyRegistry

log = logging.getLogger(__name__)

CANDLE_LOOKBACK_DAYS = 300


def _process_one(inst, broker, strategy_cls, strategy_interval, from_date, now):
    """Worker-thread entrypoint: fetch candles + run the strategy.

    A *fresh* strategy instance is built per worker — the LLM detector keeps
    per-instance call counters in `_run.calls_used`, so sharing one across
    threads would lose accounting.

    UpstoxBroker's per-process rate limiter (see `_throttle`) is thread-safe, so
    concurrent `get_historical_candles` calls from the pool don't exceed the
    configured UPSTOX_CANDLES_PER_SECOND budget.

    Returns `(instrument, candidates, error)` so the main thread can persist
    without holding the DB session across the network round-trip.
    """
    try:
        strategy = strategy_cls()
        candles = broker.get_historical_candles(
            inst.spot_instrument_key, strategy_interval, from_date, now.date()
        )
        if candles is None or candles.empty:
            return (inst, [], None)
        candidates = strategy.generate(inst, candles, now)
        return (inst, candidates, None)
    except Exception as e:  # per-instrument isolation
        return (inst, [], (str(e), traceback.format_exc()))


def run_lead_generator(broker=None, now=None, force: bool = False) -> dict | None:
    """Run one lead-generation pass. Returns `{"created": n, "checked": n}` on
    success (or `{"error": ...}` on failure); None when skipped (outside window).

    `force=True` bypasses the trading-window gate so leads can be generated
    manually from the UI (historical candles + option chains work off-hours).
    """
    now = now or health_service.now_ist()
    strategy_name = get_setting("strategy", "breakout")
    source = "scheduler.lead_generator"

    try:
        broker = broker or get_broker(Config())

        # Safety net: if the instruments whitelist is empty (e.g. the entrypoint
        # seed failed at boot), populate it so lead generation can proceed.
        if Config().AUTO_SEED_INSTRUMENTS:
            try:
                instrument_service.seed_instruments_if_empty()
            except Exception as e:
                log.warning("instrument auto-seed failed: %s", e)
                health_service.log_scheduler_error(source, e)

        # Refresh the market calendar (holidays / special sessions) once per day.
        if market_calendar.should_sync():
            try:
                market_calendar.sync_from_broker(broker)
            except Exception as e:
                log.warning("market calendar sync failed: %s", e)
                health_service.log_scheduler_error(source, e)

        if not force and not market_calendar.is_market_open(now):
            health_service.touch_heartbeat("lead_generator", "outside trading window")
            return None

        strategy_cls = StrategyRegistry.get(strategy_name)
        strategy = strategy_cls()
        from_date = now.date() - timedelta(days=CANDLE_LOOKBACK_DAYS)
        max_leads_per_run = int(get_setting("lead_generator.max_leads_per_run", 5))
        max_workers = max(1, int(get_setting("lead_generator.max_workers", 4)))
        shuffle_instruments = bool(get_setting("lead_generator.shuffle_instruments", True))
        min_days = int(get_setting("min_days_to_expiry", 5))
        lots = int(get_setting("qty_lots_per_trade", 1))
        margin_check = bool(get_setting("margin_check_enabled", True))
        max_depth = int(get_setting("margin_max_depth", get_setting("margin_strikes_below", 3)))

        # Snapshot available margin once so every lead in this run is judged
        # against the same number. Failure to fetch disables the check for the run.
        available_margin = None
        if margin_check:
            try:
                available_margin = getattr(broker.get_funds(), "available_margin", None)
            except Exception as e:
                log.warning("lead_generator: margin fetch failed (%s); margin check skipped", e)

        pending_errors = []
        created_total = checked = 0
        hit_cap = False
        scanned = 0
        strategy_interval = strategy.required_interval

        with session_scope() as session:
            instruments = session.execute(
                select(Instrument).where(Instrument.enabled.is_(True)).order_by(Instrument.id)
            ).scalars().all()
            if shuffle_instruments:
                random.shuffle(instruments)

        # Fan out (fetch candles + run strategy) across a thread pool so the
        # LLM round-trips overlap. Per-instrument DB writes stay on the main
        # thread below — SQLite serialises writers, and a single `session_scope`
        # avoids juggling locks.
        cap_lock = threading.Lock()
        results: list[tuple] = []  # (inst, candidates, error)
        with ThreadPoolExecutor(max_workers=max_workers,
                                thread_name_prefix="leadgen") as pool:
            futures = {
                pool.submit(_process_one, inst, broker, strategy_cls,
                            strategy_interval, from_date, now): inst
                for inst in instruments
            }
            try:
                for future in as_completed(futures):
                    inst = futures[future]
                    scanned += 1
                    inst_obj, candidates, err = future.result()
                    if err is not None:
                        pending_errors.append(err)
                        continue
                    results.append((inst_obj, candidates))
                    # Persist eagerly, one instrument at a time, so the lead
                    # cap (`max_leads_per_run`) is respected as soon as it is
                    # hit instead of waiting for the whole pool to drain.
                    if not candidates:
                        continue
                    with cap_lock:
                        if max_leads_per_run and created_total >= max_leads_per_run:
                            hit_cap = True
                            continue
                    with session_scope() as session:
                        created = create_leads_from_candidates(
                            session, inst_obj, candidates, now, strategy_name
                        )
                        if created:
                            attach_lead_plans(
                                session, broker, created, min_days, lots,
                                today=now.date(),
                                available_margin=available_margin, max_depth=max_depth,
                            )
                            with cap_lock:
                                created_total += len(created)
                            checked += 1
                            log.info(
                                "lead_generator: %d lead(s) for %s (%s)",
                                len(created), inst_obj.symbol, strategy_name,
                            )
            finally:
                # Drain remaining futures so workers don't leak. Any work that
                # arrived after the cap was hit is recorded but not persisted.
                if hit_cap:
                    for f in futures:
                        if not f.done():
                            f.cancel()

        # Log after the transaction commits (SQLite allows a single writer).
        for message, stack in pending_errors:
            health_service.log_error(source, message, stack)

        note = (f"strategy={strategy_name} leads={created_total}/{max_leads_per_run or 'inf'} "
                f"checked={checked} scanned={scanned}/{len(instruments)} workers={max_workers}")
        if hit_cap:
            note += " (cap_hit)"
        if force:
            note += f" manual"
        health_service.touch_heartbeat("lead_generator", note)
        return {"created": created_total, "checked": checked}
    except Exception as e:
        log.exception("lead_generator run failed")
        health_service.log_scheduler_error(source, e)
        health_service.touch_heartbeat("lead_generator", str(e)[:200], status="error")
        return {"error": str(e)}