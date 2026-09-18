"""Scheduler 3 — Order Placer.

Picks queued leads and turns them into trades:
validate (killswitch / no open trade / price divergence / option expiry >= N days)
-> resolve the option contract (ATM strike, correct CE/PE) -> place entry (LIMIT)
-> poll for fill up to `entry_order_fill_timeout_seconds` -> only if it filled does
it place the protective SL (SL, since NSE rejects SL-M for options per circular
NSE/FAOP/49677 effective 27-Sep-2021) -> persist trade + order audit.
"""

import logging
import time
import traceback

from sqlalchemy import select

from app.broker import get_broker
from app.broker.base import BrokerError, OrderRequest
from app.config import Config
from app.db import session_scope
from app.models import Lead
from app.services import contract_service, health_service, market_calendar, trade_service
from app.services.calibration import calibration_multiplier
from app.services.killswitch_service import is_killswitch_active
from app.services.lead_service import mark_lead
from app.services.score_decay import decayed_score
from app.settings import get_setting

log = logging.getLogger(__name__)

source = "scheduler.order_placer"


def run_order_placer(broker=None, now=None):
    now = now or health_service.now_ist()

    try:
        if not market_calendar.is_market_open(now):
            health_service.touch_heartbeat("order_placer", "outside trading window")
            return
        if is_killswitch_active():
            health_service.touch_heartbeat("order_placer", "killswitch active")
            return

        broker = broker or get_broker(Config())
        sl_pct = float(get_setting("initial_sl_pct", 10.0))
        max_div = float(get_setting("max_lead_price_divergence_pct", 0.5))
        min_days = int(get_setting("min_days_to_expiry", 5))
        lots = int(get_setting("qty_lots_per_trade", 1))
        margin_check = bool(get_setting("margin_check_enabled", True))
        max_depth = int(get_setting("margin_max_depth", get_setting("margin_strikes_below", 3)))
        fill_timeout = int(get_setting("entry_order_fill_timeout_seconds", 30))
        limit_premium_pct = float(get_setting("entry_limit_premium_pct", 1.0))

        available_margin = None
        if margin_check:
            try:
                available_margin = getattr(broker.get_funds(), "available_margin", None)
            except Exception as e:
                log.warning("order_placer: margin fetch failed (%s); margin check skipped", e)

        pending_errors = []
        half_life_min = float(get_setting("breakout.staleness_half_life_min", 0.0))
        calibration_alpha = float(get_setting("scoring.calibration_alpha", 0.0))
        with session_scope() as session:
            # Consume highest-confidence leads first so the best signals get filled
            # before lower-conviction ones when margin/limits constrain how many trade.
            # Tier-4: apply staleness decay + historical calibration at rank time so
            # fresh, high-quality signals outrank stale ones even if they share the
            # same stored score. Tie-break on `created_at` ascending preserves the
            # legacy "oldest first within same score" semantics.
            candidates = list(session.execute(
                select(Lead).where(Lead.status == "queued")
            ).scalars())
            def _rank_key(lead):
                decayed = decayed_score(lead, half_life_min, now)
                mult = calibration_multiplier(
                    lead.signal_type or "",
                    lead.underlying_key or "",
                    calibration_alpha,
                ) if calibration_alpha > 0 else 1.0
                return (-(decayed * mult), lead.created_at or 0)
            candidates.sort(key=_rank_key)
            for lead in candidates:
                try:
                    process_lead(session, broker, lead, sl_pct, max_div, min_days, lots, available_margin,
                                 max_depth, fill_timeout, limit_premium_pct, today=now.date())
                except Exception as e:
                    log.exception("order_placer: lead %s failed", lead.id)
                    mark_lead(session, lead, "skipped", note=str(e))
                    pending_errors.append((str(e), traceback.format_exc()))

        # Log after the transaction commits (SQLite allows a single writer).
        for message, stack in pending_errors:
            health_service.log_error(source, message, stack)

        health_service.touch_heartbeat("order_placer", "ok")
    except Exception as e:
        log.exception("order_placer run failed")
        health_service.log_scheduler_error(source, e)
        health_service.touch_heartbeat("order_placer", str(e)[:200], status="error")


def process_lead(session, broker, lead: Lead, sl_pct: float, max_div: float, min_days: int, lots: int,
                 available_margin: float | None = None, max_depth: int = 3,
                 fill_timeout: int = 30, limit_premium_pct: float = 1.0,
                 today=None) -> None:
    mark_lead(session, lead, "picked", note="processing")

    if trade_service.has_open_trade_for_underlying(session, lead.underlying_key):
        mark_lead(session, lead, "skipped", note="open trade already exists for underlying")
        return

    if trade_service.has_traded_underlying_today(session, lead.underlying_key):
        mark_lead(session, lead, "skipped", note="underlying already traded today")
        return

    ltp = (broker.get_ltp([lead.underlying_key]) or {}).get(lead.underlying_key)
    if ltp is None:
        raise BrokerError(f"no LTP for {lead.underlying_key}")
    divergence = abs(ltp - lead.signal_level) / lead.signal_level * 100.0
    if divergence > max_div:
        mark_lead(session, lead, "skipped", note=f"price diverged {divergence:.2f}% from signal")
        return

    expiry = contract_service.next_expiry(broker, lead.underlying_key, min_days, today=today)
    if expiry is None:
        raise BrokerError(f"no expiry >= {min_days} days for {lead.underlying_key}")

    # Margin-aware strike: prefer ATM; walk toward cheaper OTM until affordable.
    contracts = broker.get_option_contracts(lead.underlying_key, expiry=expiry)
    wanted = "CE" if lead.direction == "CALL" else "PE"
    matches = [c for c in contracts if c.instrument_type == wanted]
    candidates = list(contract_service.walk_candidates(matches, lead.direction, ltp, max_depth))
    premiums = broker.get_ltp([c.instrument_key for c in candidates]) or {}
    contract, cheapest_cost, evaluated = contract_service.select_affordable(candidates, premiums, available_margin, lots)
    if contract is None:
        note = "no affordable contract within margin depth"
        if evaluated and cheapest_cost is not None and available_margin is not None:
            note = f"insufficient margin: need ≥ ₹{cheapest_cost:,.0f}, available ₹{available_margin:,.0f}"
        mark_lead(session, lead, "skipped", note=note)
        return

    quantity = contract.lot_size * lots
    tag = f"lead-{lead.id}"

    # LIMIT entry: pay up to LTP + premium so a slightly stale LTP still fills.
    contract_ltp = (broker.get_ltp([contract.instrument_key]) or {}).get(contract.instrument_key)
    if contract_ltp is None:
        raise BrokerError(f"no LTP for option {contract.instrument_key}")
    instrument_tick = getattr(contract, "tick_size", 0.0) or 0.0
    raw_limit = contract_ltp * (1.0 + limit_premium_pct / 100.0)
    limit_price = trade_service.ceil_to_tick(raw_limit, trade_service.option_tick_for(raw_limit, instrument_tick))

    log.info("order_placer: placing entry order lead=%s instrument=%s | qty=%s limit=%.2f ltp=%.2f tag=%s",
             lead.id, contract.instrument_key, quantity, limit_price, contract_ltp, tag)
    entry_order_id = broker.place_order(
        OrderRequest(
            instrument_key=contract.instrument_key,
            transaction_type="BUY",
            quantity=quantity,
            product="D",
            order_type="LIMIT",
            price=limit_price,
            tag=tag,
        )
    )
    log.info("order_placer: entry order %s placed for lead=%s instrument=%s",
             entry_order_id, lead.id, contract.instrument_key)

    entry_price, status = _wait_for_fill(broker, entry_order_id, fill_timeout)
    if entry_price is None:
        _safe_cancel_if_open(broker, entry_order_id, status)
        verb = "already terminal" if status in _TERMINAL_STATUSES else "cancelled"
        raise BrokerError(
            f"entry LIMIT {entry_order_id} for {contract.instrument_key} did not fill"
            f" within {fill_timeout}s (status={status or 'unknown'}); {verb}, no trade opened"
        )

    initial_sl = trade_service.initial_sl_for(entry_price, lead.direction, sl_pct)
    instrument_tick = getattr(contract, "tick_size", 0.0) or 0.0

    try:
        sl_order_id, sl_order_type = trade_service.place_stop_loss(
            broker,
            instrument_key=contract.instrument_key,
            quantity=quantity,
            trigger_price=initial_sl,
            tag=tag,
            instrument_tick=instrument_tick,
        )
        sl_limit_price = trade_service.sl_price_below_trigger(
            initial_sl, trade_service.option_tick_for(initial_sl, instrument_tick),
        )
    except Exception as e:
        log.exception("order_placer: SL placement failed after entry fill; squaring off entry")
        sqoff_id = None
        try:
            sqoff_id = trade_service.place_defensive_sqoff(
                broker, contract.instrument_key, quantity, tag, limit_premium_pct,
            )
            log.warning("order_placer: sqoff order %s placed for %s x%s", sqoff_id, contract.instrument_key, quantity)
        except Exception as sqoff_e:
            log.error(
                "order_placer: CRITICAL — entry %s filled but SL placement (%s) AND sqoff (%s) both failed. "
                "Manual intervention required to close %s x%s. Entry order_id=%s",
                entry_order_id, e, sqoff_e, contract.instrument_key, quantity, entry_order_id,
            )
        raise BrokerError(
            f"SL placement failed for {contract.instrument_key}: {e}; "
            f"sqoff={'placed ' + sqoff_id if sqoff_id else 'FAILED — manual intervention required'}"
        )

    try:
        ok, err_msg = trade_service.verify_sl_placed(
            broker,
            sl_order_id=sl_order_id,
            intended_trigger=initial_sl,
            intended_type=sl_order_type,
            entry_price=entry_price,
        )
        if not ok:
            log.critical(
                "order_placer: SL order mismatch at broker — %s "
                "(sl_id=%s intended_trigger=%.2f intended_type=%s entry=%.2f)",
                err_msg, sl_order_id, initial_sl, sl_order_type, entry_price,
            )
            try:
                sqoff_id = trade_service.place_defensive_sqoff(
                    broker, contract.instrument_key, quantity, tag, limit_premium_pct,
                )
                log.warning("order_placer: defensive sqoff %s placed after SL mismatch", sqoff_id)
            except Exception as sqoff_e:
                log.error(
                    "order_placer: defensive sqoff after SL mismatch also failed: %s "
                    "— manual intervention required. entry_order_id=%s sl_order_id=%s",
                    sqoff_e, entry_order_id, sl_order_id,
                )
            raise BrokerError(
                f"SL order at broker failed verification: {err_msg}; "
                f"sqoff attempted for {contract.instrument_key} x{quantity}"
            )
    except BrokerError:
        raise
    except Exception as e:
        log.exception("order_placer: SL verification raised unexpectedly — proceeding")

    trade = trade_service.create_trade(
        session,
        lead=lead,
        contract=contract,
        direction=lead.direction,
        entry_price=entry_price,
        quantity=quantity,
        initial_sl=initial_sl,
        entry_order_id=entry_order_id,
        sl_order_id=sl_order_id,
    )

    trade_service.record_order(
        session, order_id=entry_order_id, trade_id=trade.id, order_type="LIMIT", transaction_type="BUY",
        instrument_token=contract.instrument_key, quantity=quantity, tag=tag, average_price=entry_price,
        tradingsymbol=contract.trading_symbol,
    )
    trade_service.record_order(
        session, order_id=sl_order_id, trade_id=trade.id, order_type=sl_order_type, transaction_type="SELL",
        instrument_token=contract.instrument_key, quantity=quantity, tag=tag, trigger_price=initial_sl,
        price=0.0 if sl_order_type == "SL-M" else sl_limit_price, tradingsymbol=contract.trading_symbol,
    )

    # Commit trade + orders FIRST so the trade survives even if the lead-status
    # update below fails. Without this split a downstream DB error rolls back
    # the whole transaction — the trade disappears while the BUY and SL
    # orders are already sitting at the broker.
    session.commit()

    try:
        with session_scope() as lead_session:
            mark_lead(lead_session, lead_session.get(Lead, lead.id), "placed", note=f"trade={trade.id}")
    except Exception as e:
        log.warning("order_placer: trade %s saved but lead status update failed: %s", trade.id, e)

    log.info("order_placer: opened trade %s for %s entry=%.2f sl=%.2f (LIMIT @ %.2f, %s)",
             trade.id, lead.underlying_key, entry_price, initial_sl, limit_price, sl_order_type)


def _wait_for_fill(broker, order_id: str, timeout_seconds: int) -> tuple[float | None, str | None]:
    """Poll `broker.get_order_book()` until the order fills, rejects, or `timeout_seconds` elapses.

    Returns `(avg_fill_price, status)`. `avg_fill_price is None` on non-fill.
    """
    deadline = time.monotonic() + timeout_seconds
    status: str | None = None
    while time.monotonic() < deadline:
        order = next((o for o in broker.get_order_book() if o.order_id == order_id), None)
        status = order.status if order else None
        if status in ("complete", "traded"):
            entry_price = trade_service.avg_fill_price(broker.get_trades_by_order(order_id))
            if entry_price is None and order and order.average_price:
                entry_price = order.average_price
            return entry_price, status
        if status in ("rejected", "cancelled"):
            return None, status
        time.sleep(1)
    return None, status


_TERMINAL_STATUSES = {"complete", "traded", "rejected", "cancelled", "canceled"}


def _safe_cancel_if_open(broker, order_id: str, known_status: str | None = None) -> None:
    """Cancel `order_id` only if it is still open. The Upstox API rejects
    cancel attempts on already-terminal orders with UDAPI100040 — those errors
    are not actionable, so we don't log them as warnings either."""
    status = known_status
    if status not in _TERMINAL_STATUSES:
        try:
            book = broker.get_order_book()
        except Exception as e:
            log.warning("order_placer: get_order_book failed during cancel check: %s", e)
            return
        order = next((o for o in book if o.order_id == order_id), None)
        status = order.status if order else None
    if status in _TERMINAL_STATUSES:
        return
    try:
        broker.cancel_order(order_id)
    except Exception as e:
        log.warning("order_placer: could not cancel order %s (status=%s): %s", order_id, status, e)