"""Scheduler 2 — Trade Tracker.

Every ~30s during the session, per open trade:
1. Auto square-off at the day's session end (respects special/half-day sessions).
2. Refresh `best_price` from live LTP.
3. Fetch the SL order status from the broker.
   - If filled/complete: the broker has already closed the position — close in DB.
   - If cancelled/rejected: the SL is gone — re-place it.
   - If still open/pending: continue.
4. Apply the trailing-SL rule (activate once price moves `trail_activate_pct`
   past the initial SL in the profitable direction; then SL = ltp ± `trail_gap_pct`
   with a ratchet) via modify_order.
5. If LTP has crossed the trailing SL, close the trade manually.

If at any point the trade has no SL order (initial placement failed, or the
SL was cancelled and re-placement failed), the tracker sqoffs the trade with
a LIMIT SELL @ LTP minus the configured premium — same defensive pattern as
the order placer's entry fallback. Manual intervention is required if even the
sqoff fails.
"""

import logging
import traceback

from sqlalchemy import select

from app.broker import get_broker
from app.broker.base import BrokerError, ModifyOrderParams, OrderRequest
from app.config import Config
from app.db import session_scope
from app.models import Trade
from app.services import health_service, market_calendar, trade_service
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
        limit_premium_pct = float(get_setting("entry_limit_premium_pct", 1.0))

        pending_errors = []
        with session_scope() as session:
            trades = session.execute(select(Trade).where(Trade.status == "open").order_by(Trade.entry_time)).scalars().all()
            for trade in trades:
                try:
                    process_trade(session, broker, trade, sl_pct, activate_pct, gap_pct, now, limit_premium_pct)
                except Exception as e:
                    log.exception("trade_tracker: trade %s failed", trade.id)
                    pending_errors.append((str(e), traceback.format_exc()))

        for message, stack in pending_errors:
            health_service.log_error(source, message, stack)

        health_service.touch_heartbeat("trade_tracker", "ok")
    except Exception as e:
        log.exception("trade_tracker run failed")
        health_service.log_scheduler_error(source, e)
        health_service.touch_heartbeat("trade_tracker", str(e)[:200], status="error")


def process_trade(session, broker, trade: Trade, sl_pct: float, activate_pct: float, gap_pct: float,
                   now, limit_premium_pct: float = 1.0) -> None:
    if now.time() >= market_calendar.session_end(now.date()):
        log.info("trade_tracker: session end sqoff for trade %s | direction=%s", trade.id, trade.direction)
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

    if trade.sl_order_id is None:
        _ensure_sl_or_sqoff(session, broker, trade, sl_pct, limit_premium_pct)
    else:
        sl_status = _sl_order_status(broker, trade.sl_order_id)
        if sl_status == "complete":
            _close_trade_from_sl_fill(session, broker, trade)
            return
        if sl_status == "cancelled":
            log.warning("trade_tracker: SL %s for trade %s was cancelled at broker; re-placing",
                        trade.sl_order_id, trade.id)
            trade.sl_order_id = None
            _ensure_sl_or_sqoff(session, broker, trade, sl_pct, limit_premium_pct)

    if trade.sl_order_id is None:
        return

    new_sl, new_state = trade_service.compute_trailing_sl(trade, ltp, activate_pct, gap_pct)
    if new_sl != trade.current_sl:
        move_sl(broker, trade, new_sl, new_state)

    if trade_service.is_sl_hit(trade, ltp):
        exit_price = trade_service.derive_exit_price(broker, trade, sqoff_id=trade.sl_order_id)
        reason = "trailing_sl" if trade.trail_state != "at_initial" else "sl_hit"
        trade_service.close_trade(session, trade, exit_price=exit_price, exit_reason=reason)
        log.info("trade_tracker: closed trade %s (%s) at %.2f", trade.id, reason, exit_price)


def _sl_order_status(broker, order_id: str) -> str | None:
    try:
        order = next((o for o in broker.get_order_book() if o.order_id == order_id), None)
    except Exception as e:
        log.warning("trade_tracker: get_order_book failed for %s: %s", order_id, e)
        return None
    return order.status if order else None


def _close_trade_from_sl_fill(session, broker, trade: Trade) -> None:
    exit_price = trade_service.derive_exit_price(broker, trade, sqoff_id=trade.sl_order_id)
    reason = "trailing_sl" if trade.trail_state != "at_initial" else "sl_hit"
    trade_service.close_trade(session, trade, exit_price=exit_price, exit_reason=reason)
    log.info("trade_tracker: SL %s filled at broker; closed trade %s (%s) at %.2f",
             trade.sl_order_id, trade.id, reason, exit_price)


def _ensure_sl_or_sqoff(session, broker, trade: Trade, sl_pct: float, limit_premium_pct: float) -> None:
    """Ensure the trade has an SL order. If placement fails, sqoff the trade
    defensively so we never hold an unprotected position. Mirrors the same
    fallback in `order_placer.process_lead`."""
    try:
        place_initial_sl(session, broker, trade, sl_pct)
        return
    except Exception as e:
        log.exception("trade_tracker: SL placement failed for trade %s; sqoffing", trade.id)
        sqoff_id = None
        try:
            sqoff_id = trade_service.place_defensive_sqoff(
                broker, trade.option_instrument_key, trade.quantity, f"trade-{trade.id}", limit_premium_pct,
            )
            log.warning("trade_tracker: sqoff order %s placed for %s x%s",
                        sqoff_id, trade.option_instrument_key, trade.quantity)
        except Exception as sqoff_e:
            log.error(
                "trade_tracker: CRITICAL — trade %s has no SL and both SL placement (%s) AND sqoff (%s) "
                "failed. Manual intervention required to close %s x%s.",
                trade.id, e, sqoff_e, trade.option_instrument_key, trade.quantity,
            )
        if sqoff_id:
            exit_price = trade_service.derive_exit_price(broker, trade, sqoff_id=sqoff_id)
            trade_service.record_order(
                session, order_id=sqoff_id, trade_id=trade.id, order_type="LIMIT", transaction_type="SELL",
                instrument_token=trade.option_instrument_key, quantity=trade.quantity,
                tag=f"trade-{trade.id}", average_price=exit_price, tradingsymbol=trade.tradingsymbol,
            )
            trade_service.close_trade(session, trade, exit_price=exit_price, exit_reason="no_sl_sqoff")


def place_initial_sl(session, broker, trade: Trade, sl_pct: float) -> None:
    if trade.sl_order_id is not None:
        return
    sl = trade_service.initial_sl_for(trade.entry_price, trade.direction, sl_pct)
    log.info("trade_tracker: placing SL for trade %s | trigger=%.2f direction=%s sl_pct=%.1f%%",
             trade.id, sl, trade.direction, sl_pct)
    order_id, sl_order_type = trade_service.place_stop_loss(
        broker,
        instrument_key=trade.option_instrument_key,
        quantity=trade.quantity,
        trigger_price=sl,
        tag=f"trade-{trade.id}",
    )
    sl_tick = trade_service.option_tick_for(sl)
    sl_limit = trade_service.sl_price_below_trigger(sl, sl_tick) if sl_order_type == "SL" else 0.0
    trade.sl_order_id = order_id
    trade.sl_order_type = sl_order_type
    trade.current_sl = sl
    log.info("trade_tracker: SL placed for trade %s | order=%s type=%s trigger=%.2f",
             trade.id, order_id, sl_order_type, sl)
    trade_service.record_order(
        session, order_id=order_id, trade_id=trade.id, order_type=sl_order_type, transaction_type="SELL",
        instrument_token=trade.option_instrument_key, quantity=trade.quantity, tag=f"trade-{trade.id}",
        trigger_price=sl, price=sl_limit,
        tradingsymbol=trade.tradingsymbol,
    )


def move_sl(broker, trade: Trade, new_sl: float, new_state: str) -> None:
    if trade.sl_order_id is None:
        return
    order_type = trade.sl_order_type or "SL-M"
    sl_tick = trade_service.option_tick_for(new_sl)
    price = 0.0 if order_type == "SL-M" else trade_service.sl_price_below_trigger(new_sl, sl_tick)
    log.info("trade_tracker: modifying SL for trade %s | order=%s old=%.2f new=%.2f type=%s",
             trade.id, trade.sl_order_id, trade.current_sl, new_sl, order_type)
    broker.modify_order(
        ModifyOrderParams(
            order_id=trade.sl_order_id,
            quantity=trade.quantity,
            trigger_price=new_sl,
            order_type=order_type,
            price=price,
            validity="DAY",
        )
    )
    trade.current_sl = new_sl
    trade.trail_state = new_state
    log.info("trade_tracker: SL modified for trade %s | order=%s new=%.2f state=%s",
             trade.id, trade.sl_order_id, new_sl, new_state)