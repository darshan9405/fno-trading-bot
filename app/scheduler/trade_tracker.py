"""Scheduler 2 — Trade Tracker.

Every ~30s during the session:
- applies the trailing-SL rule (breakeven then trail) via modify_order on the SL order,
- reconciles SL hits against live LTP,
- auto-squares-off open trades at sqoff_time (default 14:00 IST).
"""

import logging
import traceback

from sqlalchemy import select

from app.broker import get_broker
from app.broker.base import BrokerError, ModifyOrderParams, OrderRequest
from app.config import Config
from app.db import session_scope
from app.models import Trade
from app.services import health_service, market_calendar, recon_service, trade_service
from app.settings import get_setting

log = logging.getLogger(__name__)

source = "scheduler.trade_tracker"


def run_trade_tracker(broker=None, now=None):
    now = now or health_service.now_ist()

    try:
        broker = broker or get_broker(Config())
        sl_pct = float(get_setting("initial_sl_pct", 10.0))
        activate_pct = float(get_setting("trail_activate_pct", 5.0))
        gap_pct = float(get_setting("trail_gap_pct", 5.0))
        market_protection = int(get_setting("market_protection_pct", 2))

        pending_errors = []
        with session_scope() as session:
            trades = session.execute(select(Trade).where(Trade.status == "open").order_by(Trade.entry_time)).scalars().all()
            for trade in trades:
                try:
                    process_trade(session, broker, trade, sl_pct, activate_pct, gap_pct, now, market_protection)
                except Exception as e:
                    log.exception("trade_tracker: trade %s failed", trade.id)
                    pending_errors.append((str(e), traceback.format_exc()))

        # Log after the transaction commits (SQLite allows a single writer).
        for message, stack in pending_errors:
            health_service.log_error(source, message, stack)

        # Reconciliation: sync DB-open trades to the broker's real positions.
        try:
            recon_service.reconcile_open_trades(broker)
        except Exception as e:
            log.warning("recon failed: %s", e)
            health_service.log_scheduler_error(source, e)

        health_service.touch_heartbeat("trade_tracker", "ok")
    except Exception as e:
        log.exception("trade_tracker run failed")
        health_service.log_scheduler_error(source, e)
        health_service.touch_heartbeat("trade_tracker", str(e)[:200], status="error")


def process_trade(session, broker, trade: Trade, sl_pct: float, activate_pct: float, gap_pct: float, now,
                  market_protection: int = 0) -> None:
    # Auto square-off at the day's session end (respects special/half-day sessions).
    if now.time() >= market_calendar.session_end(now.date()):
        trade_service.square_off(session, broker, trade, reason="sqoff")
        return

    ltp = (broker.get_ltp([trade.option_instrument_key]) or {}).get(trade.option_instrument_key)
    if ltp is None:
        raise BrokerError(f"no LTP for {trade.option_instrument_key}")

    if trade.best_price is None:
        trade.best_price = ltp
    elif trade.direction == "CALL":
        trade.best_price = max(trade.best_price, ltp)
    else:
        trade.best_price = min(trade.best_price, ltp)

    # Fallback: ensure an SL order exists (e.g. if initial SL placement failed).
    if trade.sl_order_id is None:
        place_initial_sl(session, broker, trade, sl_pct, market_protection)

    # Trailing rule.
    new_sl, new_state = trade_service.compute_trailing_sl(trade, ltp, activate_pct, gap_pct)
    if new_sl != trade.current_sl:
        move_sl(broker, trade, new_sl, new_state, market_protection)

    # SL hit (the broker SL order should already be closing the position).
    if trade_service.is_sl_hit(trade, ltp):
        exit_price = exit_fill_price(broker, trade) or trade.current_sl
        reason = "trailing_sl" if trade.trail_state != "at_initial" else "sl_hit"
        trade_service.close_trade(session, trade, exit_price=exit_price, exit_reason=reason)
        log.info("trade_tracker: closed trade %s (%s) at %.2f", trade.id, reason, exit_price)


def place_initial_sl(session, broker, trade: Trade, sl_pct: float, market_protection: int = 0) -> None:
    if trade.sl_order_id is not None:
        return
    sl = trade_service.initial_sl_for(trade.entry_price, trade.direction, sl_pct)
    order_id = broker.place_order(
        OrderRequest(
            instrument_key=trade.option_instrument_key,
            transaction_type="SELL",
            quantity=trade.quantity,
            product="I",
            order_type="SL-M",
            trigger_price=sl,
            tag=f"trade-{trade.id}",
            market_protection=market_protection,
        )
    )
    trade.sl_order_id = order_id
    trade.current_sl = sl
    trade_service.record_order(
        session, order_id=order_id, trade_id=trade.id, order_type="SL-M", transaction_type="SELL",
        instrument_token=trade.option_instrument_key, quantity=trade.quantity, tag=f"trade-{trade.id}",
        trigger_price=sl, tradingsymbol=trade.tradingsymbol,
    )


def move_sl(broker, trade: Trade, new_sl: float, new_state: str, market_protection: int = 0) -> None:
    if trade.sl_order_id is None:
        return
    broker.modify_order(
        ModifyOrderParams(
            order_id=trade.sl_order_id,
            quantity=trade.quantity,
            trigger_price=new_sl,
            order_type="SL-M",
            price=0.0,
            validity="DAY",
            market_protection=market_protection,
        )
    )
    trade.current_sl = new_sl
    trade.trail_state = new_state


def exit_fill_price(broker, trade: Trade) -> float | None:
    if trade.sl_order_id is None:
        return None
    return trade_service.avg_fill_price(broker.get_trades_by_order(trade.sl_order_id))