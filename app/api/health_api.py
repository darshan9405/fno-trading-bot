"""System health API (Stage 7): scheduler heartbeats, errors, broker, market, auth."""

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint

from app.api.common import ok
from app.auth import UpstoxTokenStore, jwt_required
from app.broker import get_broker
from app.broker.base import BrokerError
from app.config import Config
from app.extensions import limiter
from app.services import health_service, market_calendar

IST = ZoneInfo("Asia/Kolkata")

log = logging.getLogger(__name__)

bp = Blueprint("health", __name__, url_prefix="/api/health")

# Scheduler cadences (minutes) used to judge staleness.
CADENCE_MINUTES = {"lead_generator": 30, "trade_tracker": 3, "order_placer": 3}

# Consider the Upstox token "near expiry" within this window.
TOKEN_NEAR_EXPIRY_SECONDS = 12 * 3600


@bp.get("")
@limiter.limit("120 per minute")  # dashboard polls this every 5s
@jwt_required
def health():
    now = health_service.utcnow()
    heartbeats = {}
    for name, cadence in CADENCE_MINUTES.items():
        hb = health_service.last_heartbeat(name)
        if hb is None:
            heartbeats[name] = {"status": "never_ran", "last_run_at": None, "stale": True, "note": ""}
        else:
            stale = hb.last_run_at < now - timedelta(minutes=cadence)
            heartbeats[name] = {"status": hb.status, "last_run_at": hb.last_run_at, "stale": stale, "note": hb.note}

    recent = health_service.recent_errors(limit=20)
    errors = [
        {"ts": e.ts, "source": e.source, "message": e.message[:500]}
        for e in recent
    ]

    # Broker connectivity.
    configured = bool(UpstoxTokenStore.get())
    broker_status = {"configured": configured, "connected": False, "message": "no Upstox token"}
    if configured:
        try:
            profile = get_broker(Config()).get_profile()
            broker_status["connected"] = True
            broker_status["user_id"] = profile.user_id
            broker_status["message"] = "ok"
        except BrokerError as e:
            broker_status["message"] = str(e)

    # Upstox token expiry (access tokens die at 3:30 AM IST next day).
    now = health_service.utcnow()
    exp = UpstoxTokenStore.get_expiry()
    broker_status["token_valid_until"] = exp.isoformat() if exp else None
    if exp is not None:
        seconds_left = (exp - now).total_seconds()
        broker_status["token_expires_in_sec"] = int(seconds_left)
        broker_status["token_expired"] = exp <= now
        broker_status["token_near_expiry"] = not broker_status["token_expired"] and seconds_left < TOKEN_NEAR_EXPIRY_SECONDS
    else:
        broker_status["token_expires_in_sec"] = None
        broker_status["token_expired"] = False
        broker_status["token_near_expiry"] = False

    ist_now = health_service.now_ist()
    market = {
        "open": market_calendar.is_market_open(ist_now),
        "trade_placing_open": market_calendar.is_trade_placing_window(ist_now),
        "date": ist_now.date().isoformat(),
        "time_ist": ist_now.strftime("%H:%M:%S"),
        "session": {
            "start": market_calendar.session_start(ist_now.date()).strftime("%H:%M"),
            "trade_end": market_calendar.session_trade_end(ist_now.date()).strftime("%H:%M"),
            "end": market_calendar.session_end(ist_now.date()).strftime("%H:%M"),
        },
    }

    # LLM (OpenRouter) connectivity + last-call stats.
    try:
        from app.strategy.llm_breakout import health as llm_health
        llm = llm_health.get_health_snapshot()
    except Exception as exc:  # noqa: BLE001 — never let an LLM-stats read crash /api/health
        log.warning("health: llm snapshot failed: %s", exc)
        llm = {
            "configured": False,
            "model": "",
            "base_url": "",
            "stats": {"calls_total": 0, "errors_total": 0,
                      "last_success_at": None, "last_error_at": None, "last_error": None},
            "status": "unknown",
            "error": str(exc),
        }

    return ok(
        {
            "ts": now,
            "heartbeats": heartbeats,
            "errors": {"count": len(recent), "recent": errors},
            "broker": broker_status,
            "market": market,
            "llm": llm,
        }
    )