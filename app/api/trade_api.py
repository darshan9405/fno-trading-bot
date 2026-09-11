"""Trades + P&L API (Stage 6): open/closed trades, live P&L, funds, leads."""

import logging
from datetime import timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, request
from sqlalchemy import select

from app.api.common import broker_error, error, ok
from app.auth import jwt_required
from app.broker import get_broker
from app.broker.base import BrokerError
from app.config import Config
from app.db import session_scope
from app.extensions import limiter
from app.models import Lead, Trade
from app.services.health_service import utcnow

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
    date_filter = request.args.get("date")
    status_filter = request.args.get("status")
    sort = (request.args.get("sort") or "score").lower()  # "score" | "time"
    # Build the response INSIDE the session to avoid DetachedInstanceError
    # on `lead.instrument` lazy-load after the session is gone.
    with session_scope() as session:
        q = select(Lead)
        if date_filter:
            q = q.where(Lead.created_at.like(f"{date_filter}%"))
        if status_filter:
            q = q.where(Lead.status == status_filter)
        # Tier-5: rank by score (composite confidence) so the operator sees
        # the best leads first; fall back to "time" when explicitly requested.
        if sort == "time":
            q = q.order_by(Lead.created_at.desc())
        else:
            q = q.order_by(Lead.confidence.desc(), Lead.created_at.desc())
        q = q.limit(200)
        rows = list(session.execute(q).scalars())
        data = [_lead_dict(l) for l in rows]
    return ok({"count": len(data), "leads": data})


@bp.post("/leads/generate")
@jwt_required
def generate_leads():
    """Manually run the lead generator (bypasses the trading-window gate).

    Historical candles and option chains are available off-hours, so leads can
    be generated on demand from the UI even when the market is closed.
    """
    from app.scheduler.lead_generator import run_lead_generator

    result = run_lead_generator(force=True) or {}
    if result.get("error"):
        return error("lead_generation_failed", result["error"], 502)
    return ok({"generated": result.get("created", 0), "checked": result.get("checked", 0)})


def _lead_dict(l: Lead) -> dict:
    plan = l.plan or {}
    components = l.components or {}
    # `created_at` is naive UTC. Surface IST equivalents so the UI doesn't
    # need a TZ round-trip on the client.
    ist_created = l.created_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Kolkata"))
    return {
        "id": l.id,
        "underlying": l.underlying_key,
        "symbol": l.instrument.symbol if l.instrument else None,
        "direction": l.direction,
        "strategy": l.strategy,
        "signal_type": l.signal_type,
        "signal_level": l.signal_level,
        "confidence": l.confidence,
        "components": components,
        "score_breakdown": _score_breakdown(components),
        "status": l.status,
        "note": l.note,
        "created_at": l.created_at,
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
    }


def _score_breakdown(components: dict) -> list[dict]:
    """Flatten the components dict to a list of {label, value, weight} so
    the UI can render the per-dimension contribution to the composite score."""
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
    out = []
    for k, label in labels.items():
        v = components.get(k)
        if v is None:
            continue
        out.append({
            "key": k,
            "label": label,
            "value": float(v),
            "weight": float(weights.get(k, 0.0)),
            "contribution": round(float(v) * float(weights.get(k, 0.0)), 4),
        })
    return out


