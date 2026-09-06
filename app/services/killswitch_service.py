"""Killswitch: app-level flag (persisted) that halts lead generation and order placement."""

import logging

from sqlalchemy import select

from app.db import session_scope
from app.models import KillSwitch, Trade
from app.services import health_service, trade_service
from app.services.health_service import utcnow

log = logging.getLogger(__name__)


def is_killswitch_active() -> bool:
    with session_scope() as session:
        row = session.execute(
            select(KillSwitch).where(KillSwitch.active.is_(True)).order_by(KillSwitch.id.desc())
        ).scalars().first()
        return row is not None


def activate_killswitch(reason: str | None = None, triggered_by: str = "user") -> None:
    with session_scope() as session:
        session.add(KillSwitch(reason=reason, triggered_by=triggered_by, active=True, triggered_at=utcnow()))


def release_killswitch() -> None:
    with session_scope() as session:
        for row in session.execute(select(KillSwitch).where(KillSwitch.active.is_(True))).scalars():
            row.active = False
            row.released_at = utcnow()


def current_killswitch() -> KillSwitch | None:
    with session_scope() as session:
        return session.execute(
            select(KillSwitch).where(KillSwitch.active.is_(True)).order_by(KillSwitch.id.desc())
        ).scalars().first()


def square_off_all_open_trades(broker) -> dict:
    """Exit every managed open trade (market SELL), mark `killed`.

    Each trade is squared off in its own transaction so a failure on one trade
    does not roll back the others. Returns {"squared_off": [...], "failed": [...]}.
    """
    with session_scope() as session:
        trades = session.execute(
            select(Trade).where(Trade.status == "open").order_by(Trade.entry_time)
        ).scalars().all()
        targets = [(t.id, t.tradingsymbol) for t in trades]

    squared, failed = [], []
    for trade_id, symbol in targets:
        try:
            with session_scope() as session:
                trade = session.get(Trade, trade_id)
                exit_price = trade_service.square_off(session, broker, trade, reason="killswitch")
                squared.append(
                    {
                        "trade_id": trade.id,
                        "symbol": trade.tradingsymbol,
                        "exit_price": exit_price,
                        "realized_pnl": trade.realized_pnl,
                    }
                )
        except Exception as e:  # noqa: BLE001 - isolate per-trade failures
            log.warning("square_off failed for trade %s: %s", trade_id, e)
            failed.append({"trade_id": trade_id, "symbol": symbol, "error": str(e)})

    for f in failed:
        health_service.log_error("killswitch.square_off", f"trade {f['trade_id']}: {f['error']}")
    return {"squared_off": squared, "failed": failed}