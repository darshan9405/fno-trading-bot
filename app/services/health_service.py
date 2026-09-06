"""Health/ops helpers: market hours, heartbeats, error log (Stage 7 API consumes these)."""

import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db import session_scope
from app.models import ErrorLog, SchedulerHeartbeat

IST = ZoneInfo("Asia/Kolkata")


def utcnow() -> datetime:
    """Naive UTC now (consistent with model defaults)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now_ist() -> datetime:
    """Aware IST now (for trading-window logic)."""
    return datetime.now(IST)


def is_weekday(d: datetime | None = None) -> bool:
    return (d or now_ist()).weekday() < 5


def touch_heartbeat(scheduler: str, note: str | None = None, status: str = "ok") -> None:
    with session_scope() as session:
        row = session.get(SchedulerHeartbeat, scheduler)
        if row is None:
            session.add(SchedulerHeartbeat(scheduler=scheduler, last_run_at=utcnow(), status=status, note=note))
        else:
            row.last_run_at = utcnow()
            row.status = status
            row.note = note


def log_error(source: str, message: str, stack: str | None = None) -> None:
    with session_scope() as session:
        session.add(ErrorLog(source=source, message=message, stack=stack))


def log_scheduler_error(source: str, exc: Exception) -> None:
    log_error(source, str(exc), traceback.format_exc())


def last_heartbeat(scheduler: str) -> SchedulerHeartbeat | None:
    with session_scope() as session:
        return session.get(SchedulerHeartbeat, scheduler)


def recent_errors(limit: int = 50) -> list[ErrorLog]:
    with session_scope() as session:
        return list(
            session.execute(select(ErrorLog).order_by(ErrorLog.ts.desc()).limit(limit)).scalars()
        )