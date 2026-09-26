"""Position reconciliation: sync DB trade status to the broker's actual positions.

The broker is the source of truth on whether the position is still open and on
the execution price of any exit. Two scenarios this module handles:

A. The trade is still `open` in DB but the broker no longer holds a position
   (user exited via the Upstox UI, exchange auto-squared-off, manual cancel).
   → close the trade in DB and record the exit price from any SELL fills.

B. The bot-placed SL order has completed at the broker and the position is
   already gone. We still close in DB — same as (A) but with a more specific
   closure_cause (recon_user_sl_filled vs recon_user_exit).

`reconcile_open_trades(broker)` is invoked by the `reconciler` scheduler (and by
the per-trade `process_trade` loop in trade_tracker for fast closure without
waiting for the next tick). Trades younger than `reconciler_min_age_minutes`
are skipped to avoid stomping on a fresh entry whose position hasn't propagated
to get_positions yet.
"""

import logging

from sqlalchemy import select

from app.db import session_scope
from app.models import Trade
from app.services import health_service, trade_service
from app.services.health_service import utcnow
from app.settings import get_setting

log = logging.getLogger(__name__)

DEFAULT_MIN_AGE_MINUTES = 1


def _candidate_exit(trade: Trade, broker) -> tuple[float | None, str, str | None]:
    """Best-effort exit price/closure_cause/external_order_id for a position
    the broker no longer holds. Returns (price, reason, broker_order_id).

    Priority:
      1. Bot SL order fill (if our tag matches a completed SELL in the book).
      2. User-set SL fill (any completed SELL on the same option token).
      3. Live LTP.
      4. trade.current_sl as a last-resort stub.
    """
    try:
        book = broker.get_order_book()
    except Exception as e:
        log.warning("recon_service: get_order_book failed: %s", e)
        book = []

    # Identify SELL orders for our option that are terminal.
    sell_candidates = [
        o for o in book
        if o.instrument_token == trade.option_instrument_key
        and o.transaction_type == "SELL"
        and o.status in ("complete", "traded")
    ]

    # Prefer fills from our own SL order first.
    if trade.sl_order_id:
        ours = next((o for o in sell_candidates if o.order_id == trade.sl_order_id), None)
        if ours is None:
            fills = broker.get_trades_by_order(trade.sl_order_id)
            price = trade_service.avg_fill_price(fills)
        else:
            price = ours.average_price or trade_service.avg_fill_price(
                broker.get_trades_by_order(trade.sl_order_id)
            )
        if price is not None:
            reason = "trailing_sl" if trade.trail_state != "at_initial" else "sl_hit"
            return price, reason, trade.sl_order_id

    # Fall back to any terminal SELL the user placed (their manual SL).
    if sell_candidates:
        view = sell_candidates[0]
        fills = broker.get_trades_by_order(view.order_id)
        price = view.average_price or trade_service.avg_fill_price(fills)
        if price is not None:
            return price, "recon_user_sl_filled", view.order_id

    # LTP / current_sl stub.
    try:
        ltp = (broker.get_ltp([trade.option_instrument_key]) or {}).get(trade.option_instrument_key)
    except Exception as e:
        log.warning("recon_service: get_ltp failed for %s: %s", trade.option_instrument_key, e)
        ltp = None
    return ltp or trade.current_sl, "recon_user_exit", None


def _is_held(broker, trade: Trade) -> bool:
    """True when the broker's positions API reports our qty for our token."""
    try:
        positions = broker.get_positions()
    except Exception as e:
        log.warning("recon_service: get_positions failed: %s", e)
        return True  # assume held on broker error — don't close a live trade
    pos = next(
        (p for p in positions if p.instrument_token == trade.option_instrument_token),
        None,
    )
    if pos is None:
        return False
    # Long position has positive quantity; a closed SELL ends at 0.
    return pos.quantity > 0


def _min_age_minutes() -> int:
    return int(get_setting("scheduler.reconciler_min_age_minutes", DEFAULT_MIN_AGE_MINUTES))


def reconcile_open_trades(broker) -> dict:
    """Close DB-open trades whose position is gone at the broker.

    Per-trade isolation: each trade is handled in its own session/transaction
    so a failure on one doesn't roll back the others. Trades younger than
    `_min_age_minutes()` are skipped to give Upstox time to propagate fresh
    entries (otherwise we might close a freshly-placed trade whose position
    hasn't shown up in get_positions yet).
    """
    min_age = _min_age_minutes()
    now = utcnow()

    with session_scope() as session:
        trades = session.execute(
            select(Trade).where(Trade.status == "open").order_by(Trade.entry_time)
        ).scalars().all()
        targets = [(t.id, t.tradingsymbol, t.entry_time) for t in trades]

    reconciled, skipped, failed = [], [], []
    for trade_id, symbol, entry_time in targets:
        with session_scope() as session:
            trade = session.get(Trade, trade_id)
            if trade is None or trade.status != "open":
                continue
            asset_age_min = (now - entry_time).total_seconds() / 60.0
            if asset_age_min < min_age:
                skipped.append({"trade_id": trade_id, "symbol": symbol,
                                "reason": f"age={asset_age_min:.2f}m < {min_age}m"})
                continue
            if _is_held(broker, trade):
                continue  # broker still holds the position; let trade_tracker decide
            try:
                exit_price, exit_reason, broker_order_id = _candidate_exit(trade, broker)
                if exit_price is None:
                    log.warning("recon_service: no exit price derivable for trade %s; skipping",
                                trade_id)
                    skipped.append({"trade_id": trade_id, "symbol": symbol,
                                    "reason": "no_exit_price"})
                    continue
                # Drift audit: log the closure reason + exit price we ended up
                # with. This lets the post-trade UI render "broker had a manual
                # SELL at ₹X, we recorded ₹Y" cleanly.
                try:
                    from app.services.drift_service import record_drift
                    record_drift(
                        drift_type="position_missing",
                        trade_id=trade.id,
                        instrument_token=trade.option_instrument_key,
                        severity="info",
                        detail=(f"reconciler closed trade {trade.id} ({trade.tradingsymbol}); "
                                f"reason={exit_reason} exit_price={exit_price}"),
                        expected={"status": "open"},
                        actual={"status": "closed", "reason": exit_reason,
                                "exit_price": exit_price,
                                "broker_order_id": broker_order_id},
                        source="reconciler",
                    )
                except Exception:  # noqa: BLE001 — drift logging is best-effort
                    pass
                trade_service.set_lifecycle_stage(trade, trade_service.LIFECYCLE_EXITING)
                closure_cause = (
                    trade_service.CLOSURE_CAUSE_RECON_USER_SL_FILLED
                    if exit_reason == "recon_user_sl_filled"
                    else trade_service.CLOSURE_CAUSE_RECON_USER_EXIT
                )
                # exit_reason preserves the legacy "recon" enum the UI/UI tests
                # already consume. The new granularity lives in `closure_cause`.
                trade_service.close_trade(
                    session, trade,
                    exit_price=exit_price,
                    exit_reason="recon",
                    closure_cause=closure_cause,
                )
                reconciled.append({
                    "trade_id": trade.id,
                    "symbol": trade.tradingsymbol,
                    "exit_price": exit_price,
                    "reason": exit_reason,
                    "closure_cause": closure_cause,
                    "broker_order_id": broker_order_id,
                    "realized_pnl": trade.realized_pnl,
                })
            except Exception as e:  # noqa: BLE001 - isolate per-trade failures
                log.warning("recon failed for trade %s: %s", trade_id, e)
                failed.append({"trade_id": trade_id, "symbol": symbol, "error": str(e)})

    for f in failed:
        health_service.log_error("recon", f"trade {f['trade_id']}: {f['error']}")
    if reconciled:
        log.info("recon reconciled %d open trade(s): %s",
                 len(reconciled),
                 ",".join(str(r["trade_id"]) for r in reconciled))
    return {"reconciled": reconciled, "failed": failed, "skipped": skipped}
