"""Trades + P&L API (Stage 6): open/closed trades, live P&L, funds, leads."""

import logging

from flask import Blueprint, request
from sqlalchemy import select

from app.auth import jwt_required
from app.broker import get_broker
from app.broker.base import BrokerError
from app.config import Config
from app.db import session_scope
from app.extensions import limiter
from app.models import Lead, Trade
from app.services import recon_service
from app.services.health_service import utcnow
from app.api.common import broker_error, error, ok

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
    with session_scope() as session:
        trades = list(session.execute(select(Trade).where(Trade.status == "open").order_by(Trade.entry_time)).scalars())
        keys = [t.option_instrument_key for t in trades]
        ltps = _live_ltps(broker, keys)
        data = [_trade_dict(t, ltps.get(t.option_instrument_key)) for t in trades]
    return ok({"count": len(data), "trades": data})


@bp.get("/closed")
@jwt_required
def closed_trades():
    limit = min(int(request.args.get("limit", 100)), 500)
    with session_scope() as session:
        trades = list(
            session.execute(select(Trade).where(Trade.status == "closed").order_by(Trade.exit_time.desc()).limit(limit)).scalars()
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
    with session_scope() as session:
        q = select(Lead).order_by(Lead.created_at.desc()).limit(200)
        if date_filter:
            q = q.where(Lead.created_at.like(f"{date_filter}%"))
        rows = list(session.execute(q).scalars())
        data = [
            {
                "id": l.id,
                "underlying": l.underlying_key,
                "direction": l.direction,
                "strategy": l.strategy,
                "signal_type": l.signal_type,
                "signal_level": l.signal_level,
                "confidence": l.confidence,
                "status": l.status,
                "note": l.note,
                "created_at": l.created_at,
            }
            for l in rows
        ]
    return ok({"count": len(data), "leads": data})


@bp.post("/recon")
@jwt_required
def recon():
    """Manually trigger position reconciliation (sync DB trades to broker positions)."""
    broker = get_broker(Config())
    try:
        result = recon_service.reconcile_open_trades(broker)
    except BrokerError as e:
        return broker_error(e)
    return ok({"reconciled": result["reconciled"], "failed": result["failed"]})