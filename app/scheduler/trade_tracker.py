"""Scheduler 2 — Trade Tracker.

Every ~30s during the session, per open trade:
0. If session end reached → cancel SL + market-SELL square-off (existing path).
1. Refresh `best_price` and `last_broker_check_at` from live LTP.
2. Pull the broker's order book. Sync status of our SL order into `orders`.
3. Reconcile broker truth:
   - If our SL order is `complete`/`traded` — close trade at the SL fill price.
   - If our SL order is `rejected`/`cancelled` — clear `sl_order_id`, re-place
     a fresh SL in the next tick (existing `_ensure_sl_or_sqoff` path).
   - If a SELL order on the same option_key that we DIDN'T place is open at
     the broker with a tighter `trigger_price` — adopt it as our `current_sl`
     (`sl_source = "user"`). Only tightens: never loosens.
4. Drift check on our bot SL: if the broker's `trigger_price` differs from our
   `current_sl` after a modify, adopt the broker's view (only tighten).
5. Apply trailing rule (activate past initial SL, then ltp-trail with ratchet).
   If the new SL moved by ≥ order_tick → modify_order at the broker.
6. The bot does NOT close trades on its own LTP-vs-current_sl check.
   Closure is driven exclusively by broker order status reconciliation.

If a trade has no SL order (initial placement failed, or the SL was cancelled
and re-placement failed), the tracker attempts to re-place; failing that, it
sqoffs the trade with a LIMIT SELL — same defensive pattern as order placer.
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
            trades = session.execute(
                select(Trade).where(Trade.status == "open").order_by(Trade.entry_time)
            ).scalars().all()
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
    if trade.status != "open":
        return  # idempotent: another loop already closed it

    if now.time() >= market_calendar.session_end(now.date()):
        log.info("trade_tracker: session-end sqoff for trade %s | direction=%s lifecycle=%s",
                 trade.id, trade.direction, trade.lifecycle_stage)
        trade_service.square_off(session, broker, trade, reason="sqoff",
                                 closure_cause=trade_service.CLOSURE_CAUSE_SQOFF_SESSION)
        return

    ltp = (broker.get_ltp([trade.option_instrument_key]) or {}).get(trade.option_instrument_key)
    if ltp is None:
        # If the broker doesn't even give us LTP, we can't model the rest; bail.
        raise BrokerError(f"no LTP for {trade.option_instrument_key}")

    # 1. Best-price tracking (drives the trailing math for both CALL and PUT).
    if trade.best_price is None:
        trade.best_price = ltp
    else:
        if trade.direction == "CALL":
            trade.best_price = max(trade.best_price, ltp)
        else:
            trade.best_price = min(trade.best_price, ltp)

    trade.last_broker_check_at = health_service.utcnow()

    # 2 + 3. Reconcile broker order book. This is the ONE place that closes the
    # trade. We never close on our own LTP-vs-current_sl check; the broker SL
    # order handles the LTP cross and we observe the fill here.
    if trade.lifecycle_stage == trade_service.LIFECYCLE_SL_PENDING:
        # SL was just placed; wait for the broker's open-state confirmation.
        trade.lifecycle_stage = trade_service.LIFECYCLE_SL_ACTIVE
    elif trade.lifecycle_stage == trade_service.LIFECYCLE_PLACED:
        # Tracking started before SL was placed (shouldn't normally happen, but
        # defensive: trigger _ensure_sl_or_sqoff for this tick).
        pass

    if trade.sl_order_id is None:
        _ensure_sl_or_sqoff(session, broker, trade, sl_pct, limit_premium_pct)
        if trade.status != "open":
            return
    else:
        sl_view = trade_service.sync_order_status(session, broker, trade.sl_order_id)
        if sl_view is None:
            log.warning("trade_tracker: SL %s for trade %s missing from broker order book; will re-place",
                        trade.sl_order_id, trade.id)
            trade.sl_order_id = None
            _ensure_sl_or_sqoff(session, broker, trade, sl_pct, limit_premium_pct)
            if trade.status != "open":
                return
        else:
            # Closing events from the broker order book:
            if sl_view.status in ("complete", "traded"):
                _close_from_sl_fill(session, broker, trade, sl_view)
                return
            if sl_view.status in ("rejected", "cancelled"):
                log.warning("trade_tracker: SL %s for trade %s at status=%s; re-placing",
                            trade.sl_order_id, trade.id, sl_view.status)
                trade.sl_order_id = None
                _ensure_sl_or_sqoff(session, broker, trade, sl_pct, limit_premium_pct)
                if trade.status != "open":
                    return

    if trade.sl_order_id is not None:
        # 4. Drift between broker trigger and our current_sl (single-direction).
        sl_view = None
        try:
            book = broker.get_order_book()
        except Exception as e:
            log.warning("trade_tracker: get_order_book for drift check failed: %s", e)
            book = None
        if book is not None:
            sl_view = next(
                (o for o in book if o.order_id == trade.sl_order_id), None,
            )
            if sl_view is not None:
                trade_service.drift_broker_sl(trade, sl_view)
            # 5. Adopt any user-placed tighter SL.
            for view in trade_service.collect_orders_for_trade(broker, trade):
                trade_service.adopt_external_sl(trade, view)

    # 6. Apply trailing rule.
    if trade.sl_order_id is not None and trade.sl_source == "bot":
        new_sl, new_state = trade_service.compute_trailing_sl(trade, ltp, activate_pct, gap_pct)
        if new_sl != trade.current_sl:
            _modify_sl_safely(session, broker, trade, new_sl, new_state)
    elif trade.sl_source == "user":
        # We have no bot SL anymore; broker-side SL is responsible for the
        # exit. We just keep tracking until the broker fills it. We can still
        # bump best_price (already done above) so a fresh re-adoption after a
        # broker modify would be consistent.
        pass

    # 7. Reconcile the trade's open/closed status based on broker positions
    #    (catches user UI exits that aren't accompanied by an SL fill).
    _reconcile_position(session, broker, trade)

    if trade.lifecycle_stage in (trade_service.LIFECYCLE_TRAILING, trade_service.LIFECYCLE_SL_ACTIVE):
        # promote the stage once trailing math ratcheted
        if trade.trail_state == "trailing":
            trade.lifecycle_stage = trade_service.LIFECYCLE_TRAILING


def _close_from_sl_fill(session, broker, trade: Trade, view) -> None:
    """Close the trade in DB with the broker-confirmed SL fill price.

    Uses `get_trades_by_order` if average_price wasn't synced, otherwise the
    OrderView's average_price. Closure_cause is `sl_hit`/`trailing_sl`
    depending on whether the SL moved during the trade.
    """
    fills = broker.get_trades_by_order(view.order_id)
    price = view.average_price or trade_service.avg_fill_price(fills)
    if price is None:
        # The SL says complete but the fills endpoint returned nothing — fall
        # back to LTP so we still record a meaningful realized P&L.
        price = (broker.get_ltp([trade.option_instrument_key]) or {}).get(
            trade.option_instrument_key, trade.current_sl,
        )
    reason = "trailing_sl" if trade.trail_state != "at_initial" else "sl_hit"
    closure_cause = (
        trade_service.CLOSURE_CAUSE_TRAILING_SL
        if reason == "trailing_sl"
        else trade_service.CLOSURE_CAUSE_SL_HIT
    )
    trade_service.set_lifecycle_stage(trade, trade_service.LIFECYCLE_EXITING)
    trade_service.close_trade(session, trade, exit_price=price, exit_reason=reason,
                              closure_cause=closure_cause)
    log.info("trade_tracker: SL %s filled at broker; closed trade %s (%s) at %.2f",
             view.order_id, trade.id, reason, price)


def _reconcile_position(session, broker, trade: Trade) -> None:
    """If the broker no longer holds a position for our token and our SL order
    isn't the cause, close the trade as a user-initiated exit.

    Skipped for fresh trades (within the configured `reconciler_min_age_minutes`)
    to avoid stomping on a propagating entry. Otherwise delegates work to
    `recon_service._is_held` / `_candidate_exit`.
    """
    if trade.status != "open":
        return
    from app.services.recon_service import _is_held, _candidate_exit, _min_age_minutes
    from app.services.health_service import utcnow as _now
    if (trade.last_broker_check_at or trade.entry_time) is None:
        return
    age_min = (_now() - (trade.last_broker_check_at or trade.entry_time)).total_seconds() / 60.0
    # We typically reconciled already via run_reconciler; here we only handle
    # the *per-trade tick* fast-path. Skip if too fresh.
    if age_min < _min_age_minutes() and trade.sl_order_id is None:
        return
    if _is_held(broker, trade):
        return
    exit_price, exit_reason, broker_order_id = _candidate_exit(trade, broker)
    if exit_price is None:
        return
    closure_cause = (
        trade_service.CLOSURE_CAUSE_RECON_USER_SL_FILLED
        if exit_reason == "recon_user_sl_filled"
        else trade_service.CLOSURE_CAUSE_RECON_USER_EXIT
    )
    # If our SL order's status mirrors complete but `_close_from_sl_fill` was
    # not invoked above (race), let it be the source of truth — re-check:
    if trade.sl_order_id and broker_order_id == trade.sl_order_id:
        return  # already handled by _close_from_sl_fill above
    log.info("trade_tracker: reconciling position gone for trade %s | reason=%s broker_order_id=%s",
             trade.id, exit_reason, broker_order_id)
    trade_service.set_lifecycle_stage(trade, trade_service.LIFECYCLE_EXITING)
    # Preserve the legacy "recon" exit_reason for the UI/tests; `closure_cause`
    # carries the granular reconciliation sub-reason.
    trade_service.close_trade(
        session, trade,
        exit_price=exit_price,
        exit_reason="recon",
        closure_cause=closure_cause,
    )


def _modify_sl_safely(session, broker, trade: Trade, new_sl: float, new_state: str) -> None:
    """Modify the bot SL only when it's still open. On drift (rejected modify),
    adopt the broker's current trigger as our new current_sl (only tighten).
    """
    order_type = trade.sl_order_type or "SL-M"
    sl_tick = trade_service.option_tick_for(new_sl)
    price = 0.0 if order_type == "SL-M" else trade_service.sl_price_below_trigger(new_sl, sl_tick)
    log.info("trade_tracker: modifying SL for trade %s | order=%s old=%.2f new=%.2f type=%s",
             trade.id, trade.sl_order_id, trade.current_sl, new_sl, order_type)
    try:
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
        # Fast drift check: read the broker view back and reconcile.
        try:
            book = broker.get_order_book()
        except Exception as e:
            log.warning("trade_tracker: get_order_book after modify failed: %s", e)
            book = None
        if book is not None:
            view = next((o for o in book if o.order_id == trade.sl_order_id), None)
            if view is not None:
                trade_service.sync_order_status(session, broker, view.order_id)
                trade_service.drift_broker_sl(trade, view)
                if view.status in ("complete", "traded"):
                    # Already filled before we could modify — close trade.
                    _close_from_sl_fill(session, broker, trade, view)
                    return
                if view.status in ("rejected", "cancelled"):
                    log.warning("trade_tracker: SL %s rejected/cancelled after modify attempt; re-placing",
                                trade.sl_order_id)
                    trade.sl_order_id = None
                    return
                # verify visibility of our intended trigger on the broker
                ok, err_msg = trade_service.verify_sl_placed(
                    broker,
                    sl_order_id=trade.sl_order_id,
                    intended_trigger=new_sl,
                    intended_type=order_type,
                    entry_price=trade.entry_price,
                )
                if not ok:
                    log.critical(
                        "trade_tracker: SL modify mismatch at broker for trade %s — %s "
"(intended trigger=%.2f, type=%s, entry=%.2f); adopting broker trigger",
                        trade.id, err_msg, new_sl, order_type, trade.entry_price,
                    )
                    # Drift helper above already tightened current_sl if the
                    # broker shows a different (tighter) value.
        trade.current_sl = max(float(new_sl), float(trade.current_sl or 0.0))
        trade.trail_state = new_state
        log.info("trade_tracker: SL modified for trade %s | order=%s new=%.2f state=%s",
                 trade.id, trade.sl_order_id, new_sl, new_state)
    except Exception as e:
        log.warning("trade_tracker: modify_order failed for trade %s SL %s: %s",
                    trade.id, trade.sl_order_id, e)
        # Drift back to broker truth: read whatever trigger the SL actually has.
        try:
            book = broker.get_order_book()
        except Exception:
            book = None
        if book is not None:
            view = next((o for o in book if o.order_id == trade.sl_order_id), None)
            if view is not None:
                trade_service.drift_broker_sl(trade, view)
                if view.status in ("complete", "traded"):
                    trade_service.set_lifecycle_stage(trade, trade_service.LIFECYCLE_EXITING)
                    _close_from_sl_fill(session, broker, trade, view)
                    return


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
            trade_service.set_lifecycle_stage(trade, trade_service.LIFECYCLE_EXITING)
            trade_service.close_trade(session, trade, exit_price=exit_price, exit_reason="no_sl_sqoff",
                                      closure_cause="no_sl_sqoff")


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
    ok, err_msg = trade_service.verify_sl_placed(
        broker,
        sl_order_id=order_id,
        intended_trigger=sl,
        intended_type=sl_order_type,
        entry_price=trade.entry_price,
    )
    if not ok:
        log.critical(
            "trade_tracker: SL order mismatch at broker for trade %s — %s "
            "(intended trigger=%.2f, type=%s, entry=%.2f)",
            trade.id, err_msg, sl, sl_order_type, trade.entry_price,
        )
        try:
            broker.cancel_order(order_id)
        except Exception as cancel_e:
            log.warning("trade_tracker: cancel of mismatched SL %s failed: %s", order_id, cancel_e)
        raise BrokerError(f"SL mismatch at broker: {err_msg}")
    trade.sl_order_id = order_id
    trade.sl_order_type = sl_order_type
    trade.current_sl = sl
    trade.lifecycle_stage = trade_service.LIFECYCLE_SL_ACTIVE
    log.info("trade_tracker: SL placed for trade %s | order=%s type=%s trigger=%.2f",
             trade.id, order_id, sl_order_type, sl)
    trade_service.record_order(
        session, order_id=order_id, trade_id=trade.id, order_type=sl_order_type, transaction_type="SELL",
        instrument_token=trade.option_instrument_key, quantity=trade.quantity, tag=f"trade-{trade.id}",
        trigger_price=sl, price=sl_limit,
        tradingsymbol=trade.tradingsymbol,
    )
