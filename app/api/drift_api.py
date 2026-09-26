"""Drift / reconciliation audit API.

GET  /api/drifts                  — recent drift events (defaults last 24h)
GET  /api/drifts/summary          — counts by drift_type for the UI header
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request
from sqlalchemy import func, select

from app.auth import jwt_required
from app.db import session_scope
from app.models import TradeDrift

log = logging.getLogger(__name__)

bp = Blueprint("drift", __name__, url_prefix="/api/drifts")


def _parse_dt(s: str | None, default: datetime) -> datetime:
    if not s:
        return default
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return default


@bp.get("")
@jwt_required
def list_drifts():
    """List recent drift events for the UI's post-trade monitoring tile."""
    trade_id = request.args.get("trade_id", type=int)
    drift_type = request.args.get("drift_type", type=str)
    since = _parse_dt(request.args.get("since"),
                       datetime.utcnow() - timedelta(hours=24))
    limit = request.args.get("limit", default=200, type=int)
    try:
        with session_scope() as session:
            q = select(TradeDrift).where(TradeDrift.ts >= since)
            if trade_id is not None:
                q = q.where(TradeDrift.trade_id == trade_id)
            if drift_type:
                q = q.where(TradeDrift.drift_type == drift_type)
            rows = session.execute(
                q.order_by(TradeDrift.ts.desc()).limit(max(1, min(limit, 1000)))
            ).scalars().all()
            payload = [
                {
                    "id": r.id,
                    "trade_id": r.trade_id,
                    "instrument_token": r.instrument_token,
                    "drift_type": r.drift_type,
                    "severity": r.severity,
                    "detail": r.detail,
                    "expected": r.expected,
                    "actual": r.actual,
                    "source": r.source,
                    "ts": r.ts.isoformat() + "Z" if r.ts else None,
                }
                for r in rows
            ]
            session.expunge_all()
        return jsonify({"drifts": payload, "count": len(payload)})
    except Exception as e:  # noqa: BLE001
        log.exception("drift_api: list_drifts failed: %s", e)
        return jsonify({"drifts": [], "count": 0, "error": str(e)}), 500


@bp.get("/summary")
@jwt_required
def summary():
    """Counts by drift_type and severity for the header tile (last 24h)."""
    try:
        since = datetime.utcnow() - timedelta(hours=24)
        with session_scope() as session:
            by_type = session.execute(
                select(TradeDrift.drift_type, func.count(TradeDrift.id))
                .where(TradeDrift.ts >= since)
                .group_by(TradeDrift.drift_type)
                .order_by(func.count(TradeDrift.id).desc())
            ).all()
            by_sev = session.execute(
                select(TradeDrift.severity, func.count(TradeDrift.id))
                .where(TradeDrift.ts >= since)
                .group_by(TradeDrift.severity)
                .order_by(func.count(TradeDrift.id).desc())
            ).all()
            session.expunge_all()
        return jsonify({
            "by_type": [{"drift_type": t, "count": int(c)} for t, c in by_type],
            "by_severity": [{"severity": s, "count": int(c)} for s, c in by_sev],
            "window_hours": 24,
        })
    except Exception as e:  # noqa: BLE001
        log.exception("drift_api: summary failed: %s", e)
        return jsonify({"by_type": [], "by_severity": [], "error": str(e)}), 500