"""Killswitch API (Stage 5): activate squares off managed trades + halts the system."""

import logging

from flask import Blueprint, request

from app.auth import jwt_required
from app.broker import get_broker
from app.config import Config
from app.services import killswitch_service
from app.api.common import ok

log = logging.getLogger(__name__)

bp = Blueprint("killswitch", __name__, url_prefix="/api/killswitch")


@bp.post("/activate")
@jwt_required
def activate():
    body = request.get_json(silent=True) or {}
    reason = (body.get("reason") or "user requested").strip()

    # The flag is the primary action: halt the system regardless of whether any
    # square-off succeeds. Square-off is best-effort with per-trade isolation.
    killswitch_service.activate_killswitch(reason=reason, triggered_by="user")

    squared, failed = [], []
    try:
        broker = get_broker(Config())
        result = killswitch_service.square_off_all_open_trades(broker)
        squared, failed = result["squared_off"], result["failed"]
    except Exception as e:
        log.exception("killswitch square-off failed")
        failed.append({"trade_id": None, "symbol": None, "error": str(e)})

    return ok({"active": True, "reason": reason, "squared_off": squared, "failed": failed})


@bp.post("/release")
@jwt_required
def release():
    killswitch_service.release_killswitch()
    return ok({"active": False})


@bp.get("/status")
@jwt_required
def status():
    ks = killswitch_service.current_killswitch()
    if ks is None:
        return ok({"active": False})
    return ok(
        {
            "active": True,
            "triggered_at": ks.triggered_at,
            "reason": ks.reason,
            "triggered_by": ks.triggered_by,
        }
    )