"""Trades + P&L API (Stage 6): open/closed trades, live P&L, funds, leads."""

import logging
import traceback
from datetime import timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, request
from sqlalchemy import delete, select, update

from app.api.common import broker_error, error, ok
from app.auth import jwt_required
from app.broker import get_broker
from app.broker.base import BrokerError
from app.config import Config
from app.db import session_scope
from app.extensions import limiter
from app.models import Lead, LeadScanOutcome, Trade
from app.services.health_service import utcnow

log = logging.getLogger(__name__)

bp = Blueprint("trade", __name__, url_prefix="/api/trades")


def _trade_dict(trade: Trade, ltp: float | None = None) -> dict:
    unrealised = None
    if ltp is not None and trade.status == "open":
        unrealised = round((ltp - trade.entry_price) * trade.quantity, 2)
    return {
        "id": trade.id,
        "symbol": trade.tradingsymbol,
        "underlying": trade.underlying_key,
        "direction": trade.direction,
        "entry_price": trade.entry_price,
        "ltp": ltp,
        "quantity": trade.quantity,
        "lot_size": trade.lot_size,
        "initial_sl": trade.initial_sl,
        "current_sl": trade.current_sl,
        "trail_state": trade.trail_state,
        "status": trade.status,
        "entry_time": trade.entry_time,
        "exit_time": trade.exit_time,
        "exit_price": trade.exit_price,
        "exit_reason": trade.exit_reason,
        "realized_pnl": trade.realized_pnl,
        "unrealised_pnl": unrealised,
        "lifecycle_stage": getattr(trade, "lifecycle_stage", None),
        "sl_source": getattr(trade, "sl_source", None),
        "closure_cause": getattr(trade, "closure_cause", None),
        "last_broker_check_at": getattr(trade, "last_broker_check_at", None),
    }


def _live_ltps(broker, instrument_keys: list[str]) -> dict[str, float]:
    if not instrument_keys:
        return {}
    try:
        return broker.get_ltp(instrument_keys)
    except BrokerError as e:
        log.warning("live LTP unavailable: %s", e)
        return {}


@bp.get("/open")
@limiter.limit("120 per minute")  # dashboard polls this every 5s
@jwt_required
def open_trades():
    broker = get_broker(Config())
    date_filter = request.args.get("date")
    with session_scope() as session:
        q = select(Trade).where(Trade.status == "open")
        if date_filter:
            q = q.where(Trade.entry_time.like(f"{date_filter}%"))
        trades = list(session.execute(q.order_by(Trade.entry_time)).scalars())
        keys = [t.option_instrument_key for t in trades]
        ltps = _live_ltps(broker, keys)
        data = [_trade_dict(t, ltps.get(t.option_instrument_key)) for t in trades]
    return ok({"count": len(data), "trades": data})


@bp.get("/closed")
@jwt_required
def closed_trades():
    limit = min(int(request.args.get("limit", 100)), 500)
    date_filter = request.args.get("date")
    with session_scope() as session:
        q = select(Trade).where(Trade.status == "closed")
        if date_filter:
            q = q.where(Trade.exit_time.like(f"{date_filter}%"))
        trades = list(
            session.execute(q.order_by(Trade.exit_time.desc()).limit(limit)).scalars()
        )
        data = [_trade_dict(t) for t in trades]
    return ok({"count": len(data), "trades": data})


@bp.get("/closed/<int:trade_id>")
@jwt_required
def closed_trade_detail(trade_id: int):
    """Single closed-trade detail view for the post-trade monitoring UI.

    Returns the trade dict (same shape as the list endpoint) plus the
    drift events recorded for it (any SL mismatch, position-missing,
    qty-mismatch events that fired while the trade was open).
    """
    from app.models import TradeDrift
    with session_scope() as session:
        trade = session.get(Trade, trade_id)
        if trade is None or trade.status != "closed":
            return error("not_found", f"closed trade {trade_id} not found", 404)
        data = _trade_dict(trade)
        drifts = session.execute(
            select(TradeDrift)
            .where(TradeDrift.trade_id == trade_id)
            .order_by(TradeDrift.ts.desc())
            .limit(50)
        ).scalars().all()
        data["drifts"] = [
            {
                "id": d.id,
                "drift_type": d.drift_type,
                "severity": d.severity,
                "detail": d.detail,
                "expected": d.expected,
                "actual": d.actual,
                "source": d.source,
                "ts": d.ts.isoformat() + "Z" if d.ts else None,
            }
            for d in drifts
        ]
    return ok(data)


@bp.get("/pnl")
@limiter.limit("120 per minute")  # dashboard polls this every 5s
@jwt_required
def pnl():
    broker = get_broker(Config())
    try:
        positions = broker.get_positions()
        funds = broker.get_funds()
    except BrokerError as e:
        return broker_error(e)
    unrealised = round(sum(p.unrealised or 0 for p in positions), 2)
    realised = round(sum(p.realised or 0 for p in positions), 2)
    return ok(
        {
            "unrealised": unrealised,
            "realised": realised,
            "total": round(unrealised + realised, 2),
            "available_margin": round(funds.available_margin or 0, 2),
            "open_positions": len(positions),
            "ts": utcnow(),
        }
    )


@bp.get("/leads")
@jwt_required
def leads():
    """Return all leads (queued + recently processed), sorted by composite score.

    The cleanup scheduler (`app.scheduler.lead_cleanup`) manages retention:
    - Queued leads older than `leads.retention_hours_queued` (default 24h) are removed.
    - Processed leads (placed/skipped/expired) older than `leads.retention_hours_processed`
      (default 168h = 7 days) are removed.
    The UI can display both active and skipped leads with reasons.
    """
    # Build the response INSIDE the session to avoid DetachedInstanceError
    # on `lead.instrument` lazy-load after the session is gone.
    try:
        with session_scope() as session:
            rows = list(
                session.execute(
                    select(Lead)
                    .order_by(Lead.confidence.desc(), Lead.created_at.desc())
                    .limit(200)
                ).scalars()
            )
            data = [_lead_dict(l) for l in rows]
    except Exception as e:  # noqa: BLE001
        # Don't let one bad row (None components, missing FK, schema drift)
        # turn the entire Leads page into an opaque 500. Log the full
        # traceback server-side, return a structured error with the
        # exception class + message so the UI can display something
        # actionable.
        log.exception("GET /api/trades/leads failed")
        return error(
            "internal_error",
            f"{type(e).__name__}: {e}",
            500,
        )
    return ok({"count": len(data), "leads": data})


@bp.get("/leads/<int:lead_id>")
@jwt_required
def lead_detail(lead_id: int):
    """Single-lead detail view for the UI's "Why this lead?" expander.

    Returns the full lead dict (same shape as the list endpoint) plus the
    trade row if the lead was placed, so the operator can see both the
    signal-level rationale and the resulting trade state.
    """
    with session_scope() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            return error("not_found", f"lead {lead_id} not found", 404)
        data = _lead_dict(lead)
        if lead.trade is not None:
            trade = lead.trade
            data["trade"] = {
                "id": trade.id,
                "status": trade.status,
                "entry_price": trade.entry_price,
                "entry_time": trade.entry_time.isoformat() if trade.entry_time else None,
                "quantity": trade.quantity,
                "initial_sl": trade.initial_sl,
                "current_sl": trade.current_sl,
                "exit_price": trade.exit_price,
                "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
                "exit_reason": trade.exit_reason,
                "closure_cause": trade.closure_cause,
                "realized_pnl": trade.realized_pnl,
                "sl_order_id": trade.sl_order_id,
                "sl_order_type": trade.sl_order_type,
            }
    return ok(data)


@bp.delete("/leads")
@limiter.limit("10 per minute")
@jwt_required
def delete_all_leads():
    """Purge every row in the `leads` table regardless of status.

    Trade rows that point to a deleted lead are detached (lead_id -> NULL)
    first, so the audit trail in `trades` survives intact. The frontend
    exposes this through a typed-confirmation dialog so a stray click
    cannot wipe queued signals.
    """
    with session_scope() as session:
        ids = list(session.execute(select(Lead.id)).scalars())
        if not ids:
            return ok({"deleted": 0})
        session.execute(
            update(Trade).where(Trade.lead_id.in_(ids)).values(lead_id=None)
        )
        deleted = session.execute(
            delete(Lead).where(Lead.id.in_(ids))
        ).rowcount
    log.info("purge_leads: deleted %d lead row(s)", deleted)
    return ok({"deleted": int(deleted)})


@bp.post("/leads/generate")
@jwt_required
def generate_leads():
    """Dispatch a manual lead-generation run on a background thread.

    Historical candles and option chains are available off-hours, so leads can
    be generated on demand from the UI even when the market is closed. The
    actual broker/DB work lives in
    ``app.scheduler.lead_generator.run_lead_generator(force=True)`` — running
    it inline would block this request for tens of seconds and trip the
    client-side timeout, so we hand it off to a daemon thread and return 202
    immediately. The companion ``GET /leads/generate/<job_id>`` endpoint lets
    the UI poll for terminal status.

    409: another manual run is already in flight (the existing job id is
    returned so the UI can attach to its status stream).
    """
    from app.scheduler.lead_jobs import submit_manual_job

    job, started = submit_manual_job()
    body = job.to_dict()
    if started:
        return ok(body), 202
    return (
        jsonify({
            "status": "error",
            "error": {
                "code": "lead_generation_in_progress",
                "message": "A lead-generation run is already in progress.",
            },
            "data": body,
        }),
        409,
    )


@bp.get("/leads/generate/<job_id>")
@jwt_required
def leads_generate_status(job_id: str):
    """Return the current state of a manual lead-generation job.

    The job registry is module-level and bounded (last 5 jobs), so any unknown
    ``job_id`` — including ids retired by the registry trim — returns 404.
    """
    from app.scheduler.lead_jobs import get_job

    job = get_job(job_id)
    if job is None:
        return error("lead_generation_job_not_found", f"Unknown job id: {job_id}", 404)
    return ok(job.to_dict())


@bp.get("/leads/generate/active")
@jwt_required
def leads_generate_active():
    """Return the currently-running manual lead-generation job, if any.

    The UI calls this on page load (and whenever it loses its session_state
    entry) so a tab reload while a run is in flight can re-attach to the
    same job instead of dispatching a duplicate (the POST endpoint would
    just 409 anyway, but the UX is much worse — the user sees a toast that
    says "already running" without any context).

    404: nothing manual is running right now. The caller should fall through
    to the normal "Generate now" affordance. A scheduler-driven run that
    happens to be in flight also returns 404 — those jobs aren't visible to
    the manual-attach endpoint by design (the UI shouldn't try to poll for
    status on a job_id it never received).
    """
    from app.scheduler.lead_jobs import active_job_id

    job_id = active_job_id()
    if job_id is None:
        return error("lead_generation_no_active_job", "No manual lead-generation run in progress.", 404)
    return ok({"id": job_id})


@bp.post("/leads/generate/<job_id>/cancel")
@jwt_required
def cancel_lead_generation(job_id: str):
    """Request that an in-flight manual lead-generation run stops.

    Sets the cancel flag the generator thread polls between instruments.
    The current instrument always finishes first (we don't tear down a
    half-written DB transaction); the run then transitions to its
    "cancelled" terminal status, the partial progress (scanned-so-far,
    scan_outcomes) is preserved, and the response body returns the
    current job snapshot.

    Idempotent: a second cancel for the same already-cancelled job just
    returns the same body. A cancel for a finished job returns 409 so the
    UI can stop showing the "Stop" button.
    """
    from app.scheduler.lead_jobs import get_job, request_stop

    job = get_job(job_id)
    if job is None:
        return error("lead_generation_job_not_found", f"Unknown job id: {job_id}", 404)
    if job.status != "running":
        return error(
            "lead_generation_not_running",
            f"Job {job_id} is no longer running (status={job.status}).",
            409,
        )
    request_stop(job_id)
    # Re-read the snapshot — the generator may have transitioned between
    # the request_stop call and now; either way the UI gets the latest
    # `cancel_requested` flag for free.
    job = get_job(job_id) or job
    return ok(job.to_dict())


@bp.get("/leads/scan-outcomes")
@jwt_required
def list_scan_outcomes():
    """List the latest lead-generation scan-outcome rows.

    Each row represents one underlying the generator analysed during a run
    — whether or not it produced a lead. This is the data source for the
    UI's "Scanned stocks" panel. Query params:

      - ``job_id``: only return rows stamped with this job id (use the
        id returned by ``POST /leads/generate``).
      - ``underlying_key``: optional narrow filter by underlying.
      - ``limit``: 1..500, default 200.

    Rows are sorted newest-first.
    """
    args = request.args
    job_id = args.get("job_id") or None
    underlying_key = args.get("underlying_key") or None
    try:
        limit = max(1, min(int(args.get("limit") or 200), 500))
    except (TypeError, ValueError):
        limit = 200

    with session_scope() as session:
        q = select(LeadScanOutcome).order_by(LeadScanOutcome.scanned_at.desc())
        if job_id:
            q = q.where(LeadScanOutcome.job_id == job_id)
        if underlying_key:
            q = q.where(LeadScanOutcome.underlying_key == underlying_key)
        q = q.limit(limit)
        rows = session.execute(q).scalars().all()
        return ok([_scan_outcome_dict(r) for r in rows])


@bp.get("/leads/scan-outcomes/<int:outcome_id>")
@jwt_required
def scan_outcome_detail(outcome_id: int):
    """Return one scan outcome with its full tool-call history.

    Used by the UI's expandable row in the "Scanned stocks" panel to
    render the complete LLM agent-loop transcript (every tool call +
    result) for the per-instrument scan.
    """
    with session_scope() as session:
        row = session.get(LeadScanOutcome, outcome_id)
        if row is None:
            return error("scan_outcome_not_found", f"Unknown scan outcome id: {outcome_id}", 404)
        return ok(_scan_outcome_dict(row, include_tool_calls=True))


@bp.delete("/leads/scan-outcomes")
@jwt_required
def purge_scan_outcomes():
    """Delete every ``LeadScanOutcome`` row.

    Bound to the same confirmation flow the UI uses for "Delete all leads"
    so the operator can wipe the scan history independently if needed.
    Typically the manual "Generate now" path clears these automatically
    via ``lead_generator._clear_session_data`` — this endpoint exists for
    the rare "I want to start with an empty panel but keep my queued leads"
    case.
    """
    with session_scope() as session:
        deleted = session.execute(delete(LeadScanOutcome)).rowcount or 0
    return ok({"deleted": int(deleted)})


def _scan_outcome_dict(row: LeadScanOutcome, *, include_tool_calls: bool = False) -> dict:
    """Serialise a ``LeadScanOutcome`` for the UI.

    When ``include_tool_calls`` is False (the default for the list
    endpoint) we strip the tool-call payload — it can be multi-KB and the
    list endpoint is polled every 2s. The detail endpoint passes
    ``include_tool_calls=True`` so the modal gets the full trace.
    """
    scanned_at = row.scanned_at
    if scanned_at is not None:
        ist_scanned = scanned_at.replace(tzinfo=timezone.utc).astimezone(
            ZoneInfo("Asia/Kolkata")
        )
        scanned_at_ist = ist_scanned.isoformat()
        scanned_at_ist_label = ist_scanned.strftime("%d %b %H:%M:%S")
    else:
        scanned_at_ist = None
        scanned_at_ist_label = None
    out = {
        "id": row.id,
        "job_id": row.job_id,
        "instrument_id": row.instrument_id,
        "underlying_key": row.underlying_key,
        "symbol": row.symbol,
        "scanned_at": scanned_at,
        "scanned_at_ist": scanned_at_ist,
        "scanned_at_ist_label": scanned_at_ist_label,
        "decision": row.decision,
        "lead_id": row.lead_id,
        "leads_created": row.leads_created,
        "short_reason": row.short_reason,
        "rejection_reason": row.rejection_reason,
        "rationale": row.rationale,
        "strategy": row.strategy,
        "agent_iters": row.agent_iters,
        "duration_ms": row.duration_ms,
        "confidence": row.confidence,
        "error": row.error,
    }
    if include_tool_calls:
        out["tool_calls"] = row.tool_calls or []
    return out


@bp.post("/recon")
@jwt_required
def reconcile():
    """Run the broker-truth reconciler on demand.

    Useful from the UI to immediately close DB trades whose Upstox position is
    gone (e.g., user exited at the Upstox UI). Normally the `reconciler`
    scheduler covers this every 60 s; this endpoint just exposes it manually.
    """
    from app.services import recon_service

    try:
        broker = get_broker(Config())
        result = recon_service.reconcile_open_trades(broker)
    except BrokerError as e:
        return broker_error(e)
    return ok(result)


def _lead_dict(l: Lead) -> dict:
    plan = l.plan or {}
    components = l.components or {}
    # `created_at` is naive UTC. Surface IST equivalents so the UI doesn't
    # need a TZ round-trip on the client. Be defensive against legacy
    # rows where `created_at` may be None — fall back to "epoch" rather
    # than blowing up the whole endpoint.
    if l.created_at is None:
        created_dt = datetime(1970, 1, 1)
    else:
        created_dt = l.created_at
    ist_created = created_dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Kolkata"))
    signal_level = l.signal_level
    return {
        "id": l.id,
        "underlying": l.underlying_key,
        "symbol": l.instrument.symbol if l.instrument else None,
        "direction": l.direction,
        "strategy": l.strategy,
        "signal_type": l.signal_type,
        "signal_level": signal_level,
        "signal_price": signal_level,
        "confidence": l.confidence,
        "components": components,
        "score_breakdown": _score_breakdown(components),
        "status": l.status,
        "note": l.note,
        "created_at": created_dt,
        "created_at_ist": ist_created.isoformat(),
        "created_at_ist_label": ist_created.strftime("%d %b %H:%M"),
        "expiry": plan.get("expiry"),
        "strike_price": plan.get("strike_price"),
        "option_type": plan.get("option_type"),
        "trading_symbol": plan.get("trading_symbol"),
        "quantity": plan.get("quantity"),
        "lot_size": plan.get("lot_size"),
        "premium": plan.get("premium"),
        "margin_needed": plan.get("margin_needed"),
        "spot": plan.get("spot"),
        # Lead-detail metadata: LLM rationale + slim indicator snapshot +
        # tool-call summary. The UI's "Why this lead?" expander renders this.
        "meta": l.lead_meta or {},
    }


def _score_breakdown(components: dict) -> list[dict]:
    """Flatten the components dict to a list of {label, value, weight} so
    the UI can render the per-dimension contribution to the composite score.

    Defensive against malformed component values: a string or missing key
    is silently skipped rather than raising TypeError on `float()`. The
    full row stays renderable so a single corrupt lead doesn't 500 the
    entire `/leads` endpoint.
    """
    weights = {
        "pattern_fit":     0.40,
        "volume":          0.25,
        "trend_alignment": 0.15,
        "proximity":       0.10,
        "structure":       0.10,
        "iv":              0.05,
        "oi":              0.05,
        "time_of_day":     0.05,
    }
    labels = {
        "pattern_fit":     "Pattern fit",
        "volume":          "Volume",
        "trend_alignment": "Trend alignment",
        "proximity":       "Proximity",
        "structure":       "Structure",
        "iv":              "IV",
        "oi":              "OI",
        "time_of_day":     "Time of day",
    }
    if not isinstance(components, dict):
        return []
    out = []
    for k, label in labels.items():
        v = components.get(k)
        if v is None:
            continue
        try:
            vf = float(v)
            w = float(weights.get(k, 0.0))
        except (TypeError, ValueError):
            # Component value isn't numeric (e.g. legacy row stored a string
            # or the LLM returned a dict). Skip it; don't blow up the row.
            continue
        out.append({
            "key": k,
            "label": label,
            "value": vf,
            "weight": w,
            "contribution": round(vf * w, 4),
        })
    return out


