"""Scheduler 1 — Lead Generator.

Runs inside the trading window (weekdays, non-holidays, default 10:00-14:00
IST): fetches candles for each enabled underlying in parallel, runs the
configured strategy via the StrategyRegistry, and persists candidate leads
(deduped to one batch per instrument per day). Strategy `generate()` is empty
until Stage 9 fills in the breakout detectors.

Per-run audit trail
-------------------
Every underlying the generator analyses produces a `LeadScanOutcome` row,
even when no lead was emitted. This is what backs the UI's "Scanned stocks"
panel — it gives the operator a full audit of what the LLM looked at and
*why* it didn't take a signal. The strategy's `generate()` is required to
invoke the `on_scan_result` callback once per instrument with the
`AgentResult` (signals + rejection_reason + tool_calls + duration_ms) so
the lead generator can persist both the lead (if any) and the outcome.

Stop / cancel
-------------
`lead_jobs.is_stop_requested()` is polled between futures. When the user
clicks "Stop" in the UI, the run breaks out of the per-instrument loop,
cancels the remaining futures, and returns
`{"cancelled": True, "created": ..., "checked": ..., "scanned": ...}` so
the job can transition to its "cancelled" terminal status.

Session reset
-------------
Manual UI runs (those that arrive with a `job_id`) clear the prior run's
queued `Lead` rows AND `LeadScanOutcome` rows before kicking off, so the
operator never sees stale "yesterday's candidates" mixed in with the live
run. The scheduler-driven tick is a no-op on this front — it leaves prior
rows intact because they may belong to a still-in-flight manual run.
"""

import logging
import random
import threading
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Callable

from sqlalchemy import delete, select

from app.broker import get_broker
from app.config import Config
from app.db import session_scope
from app.models import Instrument, Lead, LeadScanOutcome
from app.services import health_service, instrument_service, market_calendar
from app.services.lead_service import attach_lead_plans, create_leads_from_candidates
from app.settings import get_setting
from app.strategy import StrategyRegistry

log = logging.getLogger(__name__)

CANDLE_LOOKBACK_DAYS = 300

# Cap on how many per-instrument events we keep in the progress snapshot.
# The UI polls every 2s; sending more than 5 would dominate the payload and
# doesn't add information once a run has been running for a while.
_RECENT_CAP = 5
# Cap on how many per-instrument SCAN OUTCOMES we keep in the live progress
# snapshot. Bigger than `_RECENT_CAP` because the "Scanned stocks" panel
# wants to show the whole current run, not just the tail.
_SCAN_OUTCOMES_CAP = 200


# ---------------------------------------------------------------------------
# Per-instrument worker
# ---------------------------------------------------------------------------


def _process_one(
    inst,
    broker,
    strategy_cls,
    strategy_interval,
    from_date,
    now,
    on_tool_call=None,
):
    """Worker-thread entrypoint: fetch candles + run the strategy.

    A *fresh* strategy instance is built per worker — the LLM detector keeps
    per-instance call counters in `_run.calls_used`, so sharing one across
    threads would lose accounting.

    UpstoxBroker's per-process rate limiter (see `_throttle`) is thread-safe, so
    concurrent `get_historical_candles` calls from the pool don't exceed the
    configured UPSTOX_CANDLES_PER_SECOND budget.

    Returns `(instrument, candidates, error, scan_outcome)` so the main
    thread can persist without holding the DB session across the network
    round-trip. `scan_outcome` is the dict the strategy's `on_scan_result`
    callback produced (None when the strategy didn't call it).

    `on_tool_call(event)` (when provided) is bound to this instrument's
    symbol and forwarded into the strategy's LLM agent loop so the UI can
    watch per-tool-call progress live.
    """
    symbol = getattr(inst, "symbol", "?")
    per_symbol_cb = None
    if on_tool_call is not None:
        def per_symbol_cb(event: dict) -> None:
            try:
                on_tool_call(symbol, event)
            except Exception as cb_err:  # noqa: BLE001
                # A bad UI hook must never crash a worker thread.
                log.debug("lead_generator: on_tool_call raised: %s", cb_err)

    # Thread-local slot for the per-instrument scan outcome. The strategy's
    # `on_scan_result` callback writes into this closure; the worker
    # returns it to the main thread. None when the strategy never called
    # the callback (e.g. candle fetch failed).
    scan_outcome_box: dict = {}

    def _on_scan_result(result: dict) -> None:
        scan_outcome_box["result"] = result

    try:
        strategy = strategy_cls()
        candles = broker.get_historical_candles(
            inst.spot_instrument_key, strategy_interval, from_date, now.date()
        )
        if candles is None or candles.empty:
            return (inst, [], None, {
                "decision": "no_signal",
                "rejection_reason": "no candle history available for lookback",
                "tool_calls": [],
                "agent_iters": 0,
                "duration_ms": 0,
                "error": None,
            })
        # Hand the broker + today + lot_size down so the strategy's
        # tool-calling agent loop can reach for option-chain context if it
        # wants to. Strategies that don't need them ignore the kwargs.
        candidates = strategy.generate(
            inst,
            candles,
            now,
            broker=broker,
            today=now.date(),
            lot_size=getattr(inst, "lot_size", 1) or 1,
            on_tool_call=per_symbol_cb,
            on_scan_result=_on_scan_result,
        )
        return (inst, candidates, None, scan_outcome_box.get("result"))
    except Exception as e:  # per-instrument isolation
        return (
            inst,
            [],
            (str(e), traceback.format_exc()),
            {
                "decision": "error",
                "rejection_reason": None,
                "tool_calls": [],
                "agent_iters": 0,
                "duration_ms": 0,
                "error": str(e),
            },
        )


# ---------------------------------------------------------------------------
# Progress emitter
# ---------------------------------------------------------------------------


def _emit_progress(
    on_progress: Callable[[dict], None] | None,
    *,
    phase: str,
    scanned: int,
    total: int,
    created: int,
    checked: int,
    errors: int,
    current: str | None,
    recent: deque,
    scan_outcomes: deque,
    strategy: str,
    cancelled: bool = False,
) -> None:
    """Hand a progress snapshot to the UI callback. No-op when `on_progress`
    is None (scheduler-driven runs don't expose a job state). The callback is
    expected to be cheap and lock-safe — the caller controls thread safety."""
    if on_progress is None:
        return
    on_progress({
        "phase": phase,
        "scanned": scanned,
        "total": total,
        "created": created,
        "checked": checked,
        "errors": errors,
        "current": current,
        # `deque` isn't JSON-serialisable, and the UI only cares about the
        # tail. Materialise as a list, newest-first.
        "recent": list(recent),
        # Per-instrument scan outcomes for the "Scanned stocks" panel.
        "scan_outcomes": list(scan_outcomes),
        "strategy": strategy,
        "cancelled": cancelled,
    })


# ---------------------------------------------------------------------------
# Session reset helpers
# ---------------------------------------------------------------------------


def _clear_session_data() -> dict[str, int]:
    """Drop queued `Lead` + `LeadScanOutcome` rows for the current session.

    Called at the start of a manual run so the operator doesn't see stale
    "yesterday's leads" or "yesterday's scans" mixed in with the freshly
    generated data. Returns counts of the rows removed so the lead_jobs
    progress card can report what was cleared.

    "Current session" is intentionally a wide scope — anything that's still
    in queued / picked state for `Lead`, or unscoped for
    `LeadScanOutcome`. Anything already placed / skipped / expired (the
    `processed` buckets) is left alone because the operator expects to see
    those in the audit log even after a manual reset. The
    `LeadCleanupService` daily job will eventually age out the processed
    rows on its own retention horizon.
    """
    cleared = {"leads": 0, "scans": 0}
    try:
        with session_scope() as session:
            r = session.execute(
                delete(Lead).where(Lead.status.in_(["queued", "picked"]))
            )
            cleared["leads"] = getattr(r, "rowcount", 0) or 0
            r = session.execute(delete(LeadScanOutcome))
            cleared["scans"] = getattr(r, "rowcount", 0) or 0
        if cleared["leads"] or cleared["scans"]:
            log.info(
                "lead_generator: cleared prior session data (leads=%d, scans=%d)",
                cleared["leads"], cleared["scans"],
            )
    except Exception as e:  # noqa: BLE001
        # Best-effort cleanup — if it fails the run still proceeds and just
        # shows stale data alongside new leads. Don't kill the run over it.
        log.warning("lead_generator: session reset failed: %s", e)
    return cleared


# ---------------------------------------------------------------------------
# Scan outcome persistence
# ---------------------------------------------------------------------------


def _persist_scan_outcome(
    session,
    *,
    job_id: str | None,
    inst,
    scan_outcome: dict | None,
    leads_created: list[Lead],
    scan_started_at,
) -> LeadScanOutcome | None:
    """Materialise a `LeadScanOutcome` row from one per-instrument pass.

    `scan_outcome` is the dict the strategy's `on_scan_result` callback
    produced (may be None when the strategy didn't call the hook —
    defensive). `leads_created` is the list of `Lead` rows the lead
    generator persisted for this instrument (empty for a no-signal pass).

    The function returns the persisted row, or None when the input was so
    sparse (no scan outcome AND no leads) that there's nothing worth
    persisting. Callers should not treat None as an error.
    """
    if scan_outcome is None and not leads_created:
        return None

    decision = scan_outcome.get("decision") if scan_outcome else None
    if not decision:
        decision = "generated" if leads_created else "no_signal"

    primary_lead = leads_created[0] if leads_created else None

    # Generous size caps so the operator can read the FULL LLM reasoning
    # verbatim from the UI (no truncation anywhere on the user side).
    # Rationale / rejection_reason can run to several KB for a verbose
    # model; 64 KB each is plenty for an audit string and still well
    # within SQLite/PostgreSQL TEXT limits.
    rationale = scan_outcome.get("rationale") if scan_outcome else None
    if rationale and len(rationale) > 64_000:
        rationale = rationale[:64_000]
    rejection_reason = (
        scan_outcome.get("rejection_reason") if scan_outcome else None
    )
    if rejection_reason and len(rejection_reason) > 64_000:
        rejection_reason = rejection_reason[:64_000]
    error_text = scan_outcome.get("error") if scan_outcome else None
    if error_text and len(error_text) > 16_000:
        error_text = error_text[:16_000]

    row = LeadScanOutcome(
        job_id=job_id,
        instrument_id=getattr(inst, "id", None),
        underlying_key=getattr(inst, "spot_instrument_key", "") or "",
        symbol=getattr(inst, "symbol", "") or "?",
        decision=decision,
        lead_id=primary_lead.id if primary_lead is not None else None,
        leads_created=len(leads_created),
        rejection_reason=rejection_reason,
        rationale=rationale,
        tool_calls=scan_outcome.get("tool_calls") if scan_outcome else None,
        strategy=scan_outcome.get("strategy") if scan_outcome else None
            or "llm_breakout",
        agent_iters=int(scan_outcome.get("agent_iters") or 0) if scan_outcome else 0,
        duration_ms=int(scan_outcome.get("duration_ms") or 0) if scan_outcome else 0,
        confidence=float(
            scan_outcome.get("confidence") or 0.0
        ) if scan_outcome else 0.0,
        error=error_text,
    )
    session.add(row)
    return row


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def run_lead_generator(
    broker=None,
    now=None,
    force: bool = False,
    on_progress: Callable[[dict], None] | None = None,
    on_tool_call: Callable[[str, dict], None] | None = None,
    job_id: str | None = None,
    clear_session: bool = False,
) -> dict | None:
    """Run one lead-generation pass. Returns `{"created": n, "checked": n, ...}`
    on success (or `{"error": ...}` on failure); None when skipped (outside
    window); `{"cancelled": True, ...}` when the user asked us to stop.

    `force=True` bypasses the trading-window gate so leads can be generated
    manually from the UI (historical candles + option chains work off-hours).

    `job_id` is set when invoked from the manual-UI job path. When set, the
    run clears prior queued `Lead` + `LeadScanOutcome` rows first (unless
    `clear_session=False` for tests) so the operator sees a clean session.
    The job id is also stamped on every `LeadScanOutcome` row this run
    produces so the UI can filter by "this run only".

    `on_progress(patch)` is invoked from the main thread with progress
    snapshots at three points: once before the pool is fanned out (phase=
    "starting"), once per completed future (phase="analyzing"), and once
    after the pool drains (phase="finalizing"). The callback may be called
    many times in quick succession; callers should keep the callback cheap
    (the manual-job path locks a small dict under the registry lock).

    `on_tool_call(symbol, event)` is invoked from a worker thread every time
    the LLM agent loop finishes a tool call — used by the manual-job path to
    stream live LLM activity to the UI's progress panel. Callers must be
    thread-safe (the manual-job path uses a small lock around the shared
    progress dict).
    """
    # Imported here to avoid a circular import at module-load time — the
    # `lead_jobs` module imports `run_lead_generator` inside its worker
    # thread, which would re-enter this module.
    from app.scheduler import lead_jobs

    now = now or health_service.now_ist()
    # Fall back to the only built-in strategy that's currently registered.
    # The old `"breakout"` default trips `Unknown strategy: 'breakout'` for
    # fresh installs because that legacy package was removed.
    strategy_name = get_setting("strategy", "llm_breakout")
    source = "scheduler.lead_generator"

    cleared: dict[str, int] = {"leads": 0, "scans": 0}

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

        # Session reset: only when the manual-UI path explicitly asks for
        # it. Scheduler-driven ticks skip this so a manual run that ends
        # at 14:00 IST doesn't lose state to the next 14:01 IST tick.
        if clear_session:
            cleared = _clear_session_data()

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
        cancelled = False
        scanned = 0
        strategy_interval = strategy.required_interval
        recent: deque = deque(maxlen=_RECENT_CAP)
        scan_outcomes: deque = deque(maxlen=_SCAN_OUTCOMES_CAP)

        with session_scope() as session:
            instruments = session.execute(
                select(Instrument).where(Instrument.enabled.is_(True)).order_by(Instrument.id)
            ).scalars().all()
            if shuffle_instruments:
                random.shuffle(instruments)

        # Announce the fan-out before submitting any futures so the UI can
        # render "scanned 0 / N" immediately instead of flashing the previous
        # run's totals.
        _emit_progress(
            on_progress,
            phase="starting",
            scanned=0,
            total=len(instruments),
            created=0,
            checked=0,
            errors=0,
            current=None,
            recent=recent,
            scan_outcomes=scan_outcomes,
            strategy=strategy_name,
        )

        # Fan out (fetch candles + run strategy) across a thread pool so the
        # LLM round-trips overlap. Per-instrument DB writes stay on the main
        # thread below — SQLite serialises writers, and a single `session_scope`
        # avoids juggling locks.
        cap_lock = threading.Lock()
        results: list[tuple] = []  # (inst, candidates, error, scan_outcome)
        with ThreadPoolExecutor(max_workers=max_workers,
                                thread_name_prefix="leadgen") as pool:
            futures = {
                pool.submit(_process_one, inst, broker, strategy_cls,
                            strategy_interval, from_date, now,
                            on_tool_call=on_tool_call): inst
                for inst in instruments
            }
            try:
                for future in as_completed(futures):
                    inst = futures[future]
                    scanned += 1

                    # Honour the cancel flag between instruments. We let the
                    # CURRENT future complete (so we don't tear down a half-
                    # written DB transaction) and break out of the loop before
                    # pulling the next one.
                    if lead_jobs.is_stop_requested():
                        cancelled = True
                        log.info(
                            "lead_generator: stop requested after %d instruments; aborting",
                            scanned - 1,
                        )
                        # Record the cancellation in the progress snapshot so
                        # the UI immediately reflects "Run was stopped" even
                        # before the worker drains.
                        _emit_progress(
                            on_progress,
                            phase="analyzing",
                            scanned=scanned - 1,
                            total=len(instruments),
                            created=created_total,
                            checked=checked,
                            errors=len(pending_errors),
                            current=None,
                            recent=recent,
                            scan_outcomes=scan_outcomes,
                            strategy=strategy_name,
                            cancelled=True,
                        )
                        # Cancel remaining futures so we don't wait on them
                        # for the rest of the `as_completed` loop. asyncio
                        # `cancel()` on a `ThreadPoolExecutor` future is a
                        # no-op if it's already running; we still record
                        # whatever it produced below.
                        for f in futures:
                            if not f.done():
                                f.cancel()
                        break

                    inst_obj, candidates, err, scan_outcome = future.result()
                    if err is not None:
                        pending_errors.append(err)
                        recent.appendleft({
                            "symbol": getattr(inst_obj, "symbol", "?"),
                            "status": "error",
                            "leads": 0,
                            "error": str(err[0])[:120] if err else None,
                        })
                        # Persist the scan outcome even on error so the UI's
                        # Scanned stocks panel surfaces the failure.
                        with session_scope() as session:
                            _persist_scan_outcome(
                                session,
                                job_id=job_id,
                                inst=inst_obj,
                                scan_outcome=scan_outcome,
                                leads_created=[],
                                scan_started_at=None,
                            )
                        if scan_outcome is not None:
                            _append_scan_outcome(scan_outcomes, inst_obj, scan_outcome, [])
                        _emit_progress(
                            on_progress,
                            phase="analyzing",
                            scanned=scanned,
                            total=len(instruments),
                            created=created_total,
                            checked=checked,
                            errors=len(pending_errors),
                            current=getattr(inst_obj, "symbol", None),
                            recent=recent,
                            scan_outcomes=scan_outcomes,
                            strategy=strategy_name,
                        )
                        continue
                    results.append((inst_obj, candidates, scan_outcome))
                    # Persist eagerly, one instrument at a time, so the lead
                    # cap (`max_leads_per_run`) is respected as soon as it is
                    # hit instead of waiting for the whole pool to drain.
                    if not candidates:
                        recent.appendleft({
                            "symbol": getattr(inst_obj, "symbol", "?"),
                            "status": "empty",
                            "leads": 0,
                            "error": None,
                        })
                        with session_scope() as session:
                            _persist_scan_outcome(
                                session,
                                job_id=job_id,
                                inst=inst_obj,
                                scan_outcome=scan_outcome,
                                leads_created=[],
                                scan_started_at=None,
                            )
                        if scan_outcome is not None:
                            _append_scan_outcome(scan_outcomes, inst_obj, scan_outcome, [])
                        _emit_progress(
                            on_progress,
                            phase="analyzing",
                            scanned=scanned,
                            total=len(instruments),
                            created=created_total,
                            checked=checked,
                            errors=len(pending_errors),
                            current=getattr(inst_obj, "symbol", None),
                            recent=recent,
                            scan_outcomes=scan_outcomes,
                            strategy=strategy_name,
                        )
                        continue
                    with cap_lock:
                        if max_leads_per_run and created_total >= max_leads_per_run:
                            hit_cap = True
                            recent.appendleft({
                                "symbol": getattr(inst_obj, "symbol", "?"),
                                "status": "cap",
                                "leads": 0,
                                "error": None,
                            })
                            _emit_progress(
                                on_progress,
                                phase="analyzing",
                                scanned=scanned,
                                total=len(instruments),
                                created=created_total,
                                checked=checked,
                                errors=len(pending_errors),
                                current=getattr(inst_obj, "symbol", None),
                                recent=recent,
                                scan_outcomes=scan_outcomes,
                                strategy=strategy_name,
                            )
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
                            recent.appendleft({
                                "symbol": getattr(inst_obj, "symbol", "?"),
                                "status": "leads",
                                "leads": len(created),
                                "error": None,
                            })
                            log.info(
                                "lead_generator: %d lead(s) for %s (%s)",
                                len(created), inst_obj.symbol, strategy_name,
                            )
                        else:
                            recent.appendleft({
                                "symbol": getattr(inst_obj, "symbol", "?"),
                                "status": "empty",
                                "leads": 0,
                                "error": None,
                            })
                        _persist_scan_outcome(
                            session,
                            job_id=job_id,
                            inst=inst_obj,
                            scan_outcome=scan_outcome,
                            leads_created=created,
                            scan_started_at=None,
                        )
                        if scan_outcome is not None:
                            _append_scan_outcome(scan_outcomes, inst_obj, scan_outcome, created)
                        _emit_progress(
                            on_progress,
                            phase="analyzing",
                            scanned=scanned,
                            total=len(instruments),
                            created=created_total,
                            checked=checked,
                            errors=len(pending_errors),
                            current=getattr(inst_obj, "symbol", None),
                            recent=recent,
                            scan_outcomes=scan_outcomes,
                            strategy=strategy_name,
                        )
            finally:
                # Drain remaining futures so workers don't leak. Any work that
                # arrived after the cap was hit is recorded but not persisted.
                if hit_cap or cancelled:
                    for f in futures:
                        if not f.done():
                            f.cancel()

        # Final snapshot — fires once the pool has fully drained but before
        # error logging, so the UI can show the "finishing up" state.
        _emit_progress(
            on_progress,
            phase="finalizing",
            scanned=scanned,
            total=len(instruments),
            created=created_total,
            checked=checked,
            errors=len(pending_errors),
            current=None,
            recent=recent,
            scan_outcomes=scan_outcomes,
            strategy=strategy_name,
            cancelled=cancelled,
        )

        # Log after the transaction commits (SQLite allows a single writer).
        for message, stack in pending_errors:
            health_service.log_error(source, message, stack)

        note = (f"strategy={strategy_name} leads={created_total}/{max_leads_per_run or 'inf'} "
                f"checked={checked} scanned={scanned}/{len(instruments)} workers={max_workers}")
        if hit_cap:
            note += " (cap_hit)"
        if force:
            note += f" manual"
        if cancelled:
            note += " (cancelled)"
        if cleared.get("leads") or cleared.get("scans"):
            note += f" cleared_prior=leads{cleared['leads']},scans{cleared['scans']}"
        health_service.touch_heartbeat("lead_generator", note)
        return {
            "created": created_total,
            "checked": checked,
            "scanned": scanned,
            "total": len(instruments),
            "cancelled": cancelled,
            "cleared": cleared,
        }
    except Exception as e:
        log.exception("lead_generator run failed")
        health_service.log_scheduler_error(source, e)
        health_service.touch_heartbeat("lead_generator", str(e)[:200], status="error")
        return {"error": str(e)}


def _append_scan_outcome(
    scan_outcomes: deque,
    inst,
    scan_outcome: dict,
    created: list[Lead],
) -> None:
    """Push one per-instrument scan result into the live progress deque.

    The deque is rendered into the UI's "Scanned stocks" panel via the
    2-second polling cycle, so each entry is shaped to match what the UI
    expects: symbol, decision (generated/no_signal/error), the LLM's
    rejection_reason (truncated to a UI-friendly 200 chars), and a short
    rationale snippet. Full tool-call detail is fetched from the
    `LeadScanOutcome` row when the user expands a row.
    """
    decision = scan_outcome.get("decision") or (
        "generated" if created else "no_signal"
    )
    rejection_reason = scan_outcome.get("rejection_reason")
    if rejection_reason:
        rejection_reason = str(rejection_reason)
        if len(rejection_reason) > 200:
            rejection_reason = rejection_reason[:200] + "…"
    rationale = scan_outcome.get("rationale")
    if rationale:
        rationale = str(rationale)
        if len(rationale) > 200:
            rationale = rationale[:200] + "…"
    error_text = scan_outcome.get("error")
    if error_text:
        error_text = str(error_text)
        if len(error_text) > 200:
            error_text = error_text[:200] + "…"
    scan_outcomes.appendleft({
        "symbol": getattr(inst, "symbol", "?") or "?",
        "underlying_key": getattr(inst, "spot_instrument_key", "") or "",
        "instrument_id": getattr(inst, "id", None),
        "decision": decision,
        "leads_created": len(created),
        "rejection_reason": rejection_reason,
        "rationale": rationale,
        "agent_iters": int(scan_outcome.get("agent_iters") or 0),
        "duration_ms": int(scan_outcome.get("duration_ms") or 0),
        "error": error_text,
    })