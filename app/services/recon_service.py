"""Position reconciliation: sync DB trade status to the broker's actual positions.

If the broker no longer holds a position for a DB-open trade (SL hit, manual
exit, killswitch, or an order that executed without our DB update), the trade is
closed in the DB so the two stay in sync. Each trade is handled in its own
transaction (isolation).
"""

import logging

from sqlalchemy import select

from app.db import session_scope
from app.models import Trade
from app.services import health_service, trade_service

log = logging.getLogger(__name__)

# Only reconcile trades that have been open this long (avoids closing a freshly
# opened trade whose position hasn't propagated to get_positions yet).
RECON_MIN_AGE_MINUTES = 5


def _exit_from_broker(broker, trade) -> tuple[float, str]:
    """Best-effort exit price/reason for a position the broker no longer holds."""
    # Exact fill from the SL order if it executed.
    if trade.sl_order_id:
        fills = broker.get_trades_by_order(trade.sl_order_id)
        price = trade_service.avg_fill_price(fills)
        if price is not None:
            reason = "trailing_sl" if trade.trail_state != "at_initial" else "sl_hit"
            return price, reason
    # Live LTP as an approximation.
    ltp = (broker.get_ltp([trade.option_instrument_key]) or {}).get(trade.option_instrument_key)
    if ltp is not None:
        return ltp, "recon"
    return trade.current_sl, "recon"


def reconcile_open_trades(broker) -> dict:
    """Close DB-open trades whose position is gone at the broker. Per-trade isolation."""
    positions = broker.get_positions()
    pos_map = {p.instrument_token: p for p in positions}
    now = health_service.utcnow()

    with session_scope() as session:
        trades = session.execute(
            select(Trade).where(Trade.status == "open").order_by(Trade.entry_time)
        ).scalars().all()
        targets = [(t.id, t.tradingsymbol, t.option_instrument_token, t.entry_time) for t in trades]

    reconciled, failed = [], []
    for trade_id, symbol, token, entry_time in targets:
        pos = pos_map.get(token)
        if pos is not None and pos.quantity != 0:
            continue  # still held
        if (now - entry_time).total_seconds() < RECON_MIN_AGE_MINUTES * 60:
            continue  # too fresh; position may not have propagated yet
        try:
            with session_scope() as session:
                trade = session.get(Trade, trade_id)
                exit_price, reason = _exit_from_broker(broker, trade)
                trade_service.close_trade(session, trade, exit_price=exit_price, exit_reason=reason)
                reconciled.append(
                    {
                        "trade_id": trade.id,
                        "symbol": trade.tradingsymbol,
                        "exit_price": exit_price,
                        "reason": reason,
                        "realized_pnl": trade.realized_pnl,
                    }
                )
        except Exception as e:  # noqa: BLE001 - isolate per-trade failures
            log.warning("recon failed for trade %s: %s", trade_id, e)
            failed.append({"trade_id": trade_id, "symbol": symbol, "error": str(e)})

    for f in failed:
        health_service.log_error("recon", f"trade {f['trade_id']}: {f['error']}")
    if reconciled:
        log.info("recon reconciled %d open trade(s)", len(reconciled))
    return {"reconciled": reconciled, "failed": failed}