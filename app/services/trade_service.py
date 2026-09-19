"""Trade persistence + trailing-SL math shared by order_placer and trade_tracker.

Lifecycle model (DB-side, drives trade_tracker branching):

  placed      — order_placer persisted the trade after entry fill, SL not yet placed.
  sl_pending  — SL order placed at the broker but tracker has not yet observed its open state.
  sl_active   — SL at the broker is open with our intended trigger. Protective.
  trailing    — compute_trailing_sl has flipped the trade into trailing mode at least once.
  exiting     — broker order flipped terminal (complete/rejected/cancelled) or position is gone.
  closed      — terminal (status="closed", exit_* populated).

The tracker takes the broker as the source of truth on closure: it never closes a
trade based on its own LTP-vs-current_sl check (the broker SL trigger handles the
exit). Adoption of human-modified SL is single-direction: only tighten, never loosen.
"""

import logging

from sqlalchemy import select

from app.broker.base import BrokerError, InstrumentView, OrderRequest, OrderView, normalize_instrument_tick
from app.db import session_scope
from app.models import Lead, Order, OrderFill, Trade
from app.services.health_service import utcnow

log = logging.getLogger(__name__)

PRODUCT = "D"  # delivery / NRML for F&O (Upstox product code for carry-forward)

DEFAULT_OPTION_TICK = 0.05
FNO_OPTION_TICK = 0.05  # NSE F&O options: uniform ₹0.05 tick across all strikes/premiums.

LIFECYCLE_PLACED = "placed"
LIFECYCLE_SL_PENDING = "sl_pending"
LIFECYCLE_SL_ACTIVE = "sl_active"
LIFECYCLE_TRAILING = "trailing"
LIFECYCLE_EXITING = "exiting"
LIFECYCLE_CLOSED = "closed"

CLOSURE_CAUSE_SL_HIT = "sl_hit"
CLOSURE_CAUSE_TRAILING_SL = "trailing_sl"
CLOSURE_CAUSE_SQOFF_SESSION = "sqoff_session"
CLOSURE_CAUSE_RECON_USER_EXIT = "recon_user_exit"
CLOSURE_CAUSE_RECON_USER_SL_FILLED = "recon_user_sl_filled"
CLOSURE_CAUSE_KILLSWITCH = "killswitch"


def _token_from_key(instrument_key: str) -> str:
    return instrument_key.split("|")[-1]


def option_tick_for(price: float, instrument_tick: float = 0.0) -> float:
    """NSE F&O options have a flat ₹0.05 tick. Trust the broker-reported tick
    when sane (sanity-clipped to rupees); fall back to FNO_OPTION_TICK."""
    return normalize_instrument_tick(instrument_tick)


def round_to_tick(price: float, tick: float = DEFAULT_OPTION_TICK) -> float:
    """Snap a price to the nearest tick (must be > 0)."""
    tick = tick or DEFAULT_OPTION_TICK
    return round(round(price / tick) * tick, 2)


def ceil_to_tick(price: float, tick: float = DEFAULT_OPTION_TICK) -> float:
    """Snap a price UP to the next tick (used for entry LIMIT offers so we
    always bid ≥ the current LTP — rounding down could price us below LTP
    and the order never fills)."""
    import math
    tick = tick or DEFAULT_OPTION_TICK
    return round(math.ceil(price / tick - 1e-9) * tick, 2)


def sl_price_below_trigger(trigger: float, tick: float = DEFAULT_OPTION_TICK) -> float:
    """Return a SL limit price strictly less than `trigger` (Upstox UDAPI1038
    rejects SELL SL orders where price == trigger_price). `tick` is the option's
    tick at the trigger price; the limit is exactly one tick below."""
    raw = trigger - tick
    sl_limit = round(raw, 2)
    if sl_limit < 0.05:
        sl_limit = 0.05
    return sl_limit


# Substrings the Upstox SDK returns in the ApiException body when SL-M is
# rejected for F&O options (historical NSE/FAOP/49677). Used as a soft hint
# to fall back to a stop-loss-limit (SL) order.
_SLM_REJECT_HINTS = ("SL-M", "SLM", "stop loss market", "stop-loss market", "SL M")


def _is_slm_rejection(err: Exception) -> bool:
    """Heuristic: did the broker reject because SL-M is unsupported for this
    segment? Used to fall back to SL (limit < trigger)."""
    msg = (str(err) or "") + " " + (getattr(err, "api_message", "") or "")
    msg = msg.lower()
    return any(h.lower() in msg for h in _SLM_REJECT_HINTS)


def place_stop_loss(
    broker,
    *,
    instrument_key: str,
    quantity: int,
    trigger_price: float,
    tag: str,
    instrument_tick: float = 0.0,
) -> tuple[str, str]:
    """Place the protective SL order. Tries SL-M (cleaner: no UDAPI1038 since
    there is no limit price) and falls back to SL with limit = trigger - 1 tick
    if the broker rejects SL-M for the segment.

    Returns `(order_id, order_type)` where order_type is "SL-M" or "SL".
    Raises BrokerError if both attempts fail.
    """
    trigger = round_to_tick(trigger_price, option_tick_for(trigger_price, instrument_tick))
    log.info("trade_service: placing SL for %s | direction=SELL qty=%s type=SL-M trigger=%.2f tag=%s",
             instrument_key, quantity, trigger, tag)
    # First attempt: SL-M (Stop-Loss-Market). Becomes a market sell at trigger.
    try:
        order_id = broker.place_order(
            OrderRequest(
                instrument_key=instrument_key,
                transaction_type="SELL",
                quantity=quantity,
                product=PRODUCT,
                order_type="SL-M",
                price=0.0,
                trigger_price=trigger,
                tag=tag,
            )
        )
        log.info("trade_service: SL-M placed order_id=%s for %s", order_id, instrument_key)
        return order_id, "SL-M"
    except Exception as e:
        if not _is_slm_rejection(e):
            raise
        log.info(
            "trade_service: SL-M rejected by broker for %s (%s); falling back to SL with limit<trigger",
            instrument_key, str(e)[:120],
        )

    # Fallback: SL with limit strictly below trigger (UDAPI1038).
    tick_for_limit = option_tick_for(trigger, instrument_tick)
    sl_limit = sl_price_below_trigger(trigger, tick_for_limit)
    if sl_limit > 0.05 and abs((trigger - sl_limit) - tick_for_limit) > 1e-6:
        log.warning("trade_service: SL fallback gap != tick trigger=%.2f limit=%.2f tick=%.4f",
                    trigger, sl_limit, tick_for_limit)
    log.info("trade_service: placing SL fallback for %s | direction=SELL qty=%s type=SL price=%.2f trigger=%.2f tag=%s",
             instrument_key, quantity, sl_limit, trigger, tag)
    order_id = broker.place_order(
        OrderRequest(
            instrument_key=instrument_key,
            transaction_type="SELL",
            quantity=quantity,
            product=PRODUCT,
            order_type="SL",
            price=sl_limit,
            trigger_price=trigger,
            tag=tag,
        )
    )
    log.info("trade_service: SL placed order_id=%s for %s", order_id, instrument_key)
    return order_id, "SL"


def verify_sl_placed(
    broker,
    *,
    sl_order_id: str,
    intended_trigger: float,
    intended_type: str,
    entry_price: float,
) -> tuple[bool, str]:
    """Pull the just-placed SL order from the broker and compare against intent.

    For SELL protective SL on a long option, invariants:
      - transaction_type == "SELL"
      - trigger_price == intended_trigger
      - if order_type == "SL" (limit fallback): price < trigger and gap == 1 tick
      - if order_type == "SL-M": trigger < entry_price (downside protection)

    Returns (ok, message). `message` is empty on success.
    """
    try:
        order = next((o for o in broker.get_order_book() if o.order_id == sl_order_id), None)
    except Exception as e:
        return False, f"get_order_book failed: {e}"
    if order is None:
        return False, "order not in broker order book"
    if order.transaction_type != "SELL":
        return False, f"transaction_type={order.transaction_type!r} (expected 'SELL')"
    broker_trigger = order.trigger_price
    if broker_trigger is None or abs(broker_trigger - intended_trigger) > 1e-6:
        return False, (f"trigger mismatch: broker={broker_trigger} intended={intended_trigger}")
    if intended_type == "SL-M":
        if order.order_type != "SL-M":
            return False, f"order_type={order.order_type!r} (expected 'SL-M')"
        if broker_trigger >= entry_price:
            return False, f"SL-M trigger {broker_trigger} not below entry {entry_price}"
        return True, ""
    if intended_type == "SL":
        if order.order_type != "SL":
            return False, f"order_type={order.order_type!r} (expected 'SL')"
        price = order.price
        if price is None or price >= broker_trigger:
            return False, f"SL limit {price} not strictly below trigger {broker_trigger}"
        return True, ""
    return False, f"unknown intended_type {intended_type!r}"


def place_defensive_sqoff(
    broker,
    instrument_key: str,
    quantity: int,
    tag: str,
    limit_premium_pct: float,
) -> str:
    """Place a defensive LIMIT SELL at LTP minus `limit_premium_pct`%.

    Used as a fallback when the bot cannot place or maintain a protective SL —
    shared by `order_placer.process_lead` (after entry fill + SL placement failure)
    and `trade_tracker._ensure_sl_or_sqoff` (when an open trade has no SL).

    Returns the sqoff order_id. Raises `BrokerError` if there is no LTP or the
    broker rejects the order; callers wrap in their own try/except for logging
    and policy.
    """
    sqoff_price = (broker.get_ltp([instrument_key]) or {}).get(instrument_key)
    if sqoff_price is None:
        raise BrokerError(f"no LTP for sqoff of {instrument_key}")
    log.info("trade_service: placing defensive sqoff for %s qty=%s premium=%.1f%% price=%.2f tag=%s",
             instrument_key, quantity, limit_premium_pct, sqoff_price, tag)
    return broker.place_order(
        OrderRequest(
            instrument_key=instrument_key,
            transaction_type="SELL",
            quantity=quantity,
            product=PRODUCT,
            order_type="LIMIT",
            price=round(sqoff_price * (1.0 - limit_premium_pct / 100.0), 2),
            tag=tag,
        )
    )


def derive_exit_price(broker, trade: Trade, sqoff_id: str | None = None) -> float:
    """Best-effort exit price after a sqoff/close: prefer fills, fall back to LTP,
    then `current_sl`. Used by the defensive sqoff paths in order_placer and trade_tracker."""
    if sqoff_id:
        filled = avg_fill_price(broker.get_trades_by_order(sqoff_id))
        if filled is not None:
            return filled
    ltp = (broker.get_ltp([trade.option_instrument_key]) or {}).get(trade.option_instrument_key)
    return ltp or trade.current_sl


def create_trade(session, *, lead: Lead, contract: InstrumentView, direction: str, entry_price: float,
                 quantity: int, initial_sl: float, entry_order_id: str, sl_order_id: str | None = None) -> Trade:
    trade = Trade(
        lead_id=lead.id,
        underlying_key=lead.underlying_key,
        option_instrument_key=contract.instrument_key,
        option_instrument_token=_token_from_key(contract.instrument_key),
        tradingsymbol=contract.trading_symbol,
        lot_size=contract.lot_size,
        product=PRODUCT,
        direction=direction,
        entry_price=entry_price,
        quantity=quantity,
        initial_sl=initial_sl,
        current_sl=initial_sl,
        trail_state="at_initial",
        best_price=entry_price,
        status="open",
        lifecycle_stage=LIFECYCLE_PLACED if sl_order_id is None else LIFECYCLE_SL_PENDING,
        sl_source="bot" if sl_order_id is not None else None,
        entry_order_id=entry_order_id,
        sl_order_id=sl_order_id,
        entry_time=utcnow(),
    )
    session.add(trade)
    session.flush()
    return trade


def record_order(session, *, order_id: str, trade_id: int, order_type: str, transaction_type: str,
                 instrument_token: str, quantity: int, tag: str | None, status: str = "complete",
                 price: float = 0.0, average_price: float | None = None,
                 trigger_price: float | None = None, tradingsymbol: str | None = None) -> Order:
    order = Order(
        order_id=order_id,
        trade_id=trade_id,
        order_type=order_type,
        variety="regular",
        transaction_type=transaction_type,
        product=PRODUCT,
        price=price,
        trigger_price=trigger_price,
        average_price=average_price,
        quantity=quantity,
        filled_quantity=quantity if status == "complete" else 0,
        instrument_token=instrument_token,
        tradingsymbol=tradingsymbol,
        exchange="NSE",
        validity="DAY",
        is_amo=False,
        tag=tag,
        status=status,
    )
    session.add(order)
    session.flush()
    return order


def record_fill(session, *, upstox_trade_id: str, order_id: str, instrument_token: str, transaction_type: str,
                quantity: int, average_price: float, exchange_timestamp=None) -> None:
    session.add(
        OrderFill(
            upstox_trade_id=upstox_trade_id,
            order_id=order_id,
            instrument_token=instrument_token,
            transaction_type=transaction_type,
            quantity=quantity,
            average_price=average_price,
            exchange_timestamp=exchange_timestamp,
        )
    )


def get_open_trades() -> list[Trade]:
    with session_scope() as session:
        return list(session.execute(select(Trade).where(Trade.status == "open").order_by(Trade.entry_time)).scalars())


def has_open_trade_for_underlying(session, underlying_key: str) -> bool:
    return session.execute(
        select(Trade).where(Trade.underlying_key == underlying_key, Trade.status == "open")
    ).scalars().first() is not None


def has_traded_underlying_today(session, underlying_key: str) -> bool:
    """True when the underlying already has any trade today (open or closed)."""
    from datetime import datetime, time as dtime

    day_start = datetime.combine(utcnow().date(), dtime.min)
    return session.execute(
        select(Trade).where(Trade.underlying_key == underlying_key, Trade.created_at >= day_start)
    ).scalars().first() is not None


def close_trade(session, trade: Trade, *, exit_price: float, exit_reason: str,
                closure_cause: str | None = None) -> None:
    trade.status = "closed"
    trade.exit_time = utcnow()
    trade.exit_price = exit_price
    trade.exit_reason = exit_reason
    if closure_cause:
        trade.closure_cause = closure_cause
    trade.lifecycle_stage = LIFECYCLE_CLOSED
    trade.realized_pnl = (exit_price - trade.entry_price) * trade.quantity
    # Tier-4 calibration: record this outcome so future leads carrying the
    # same (pattern, underlying) get a calibrated score at rank time.
    if trade.lead_id:
        try:
            lead = session.get(__import__("app.models").Lead, trade.lead_id) if False else None
        except Exception:
            lead = None
        try:
            from app.models import Lead as _Lead
            lead = session.get(_Lead, trade.lead_id) if trade.lead_id else None
            if lead is not None:
                from app.services.calibration import upsert_pattern_stat
                upsert_pattern_stat(
                    session,
                    pattern=lead.signal_type,
                    underlying_key=trade.underlying_key,
                )
        except Exception as e:
            log.warning("trade_service: calibration upsert failed for trade %s (%s)", trade.id, e)


def square_off(session, broker, trade: Trade, reason: str = "sqoff",
               closure_cause: str | None = CLOSURE_CAUSE_SQOFF_SESSION) -> float:
    """Exit an open trade with a market SELL, record the order, close the trade.

    Cancels the open SL first to prevent a double-exit (the broker SL trigger
    could fire milliseconds after we send the cancel). The cancel is best-effort:
    a failure to cancel is logged but does NOT stop the square-off — leaving the
    SL armed would still close the position (it just means a possible second
    order that we'll ignore via idempotent close).
    """
    log.info("trade_service: defensive square-off trade %s | reason=%s qty=%s price=%.2f",
             trade.id, reason, trade.quantity, trade.entry_price)
    if trade.sl_order_id:
        try:
            broker.cancel_order(trade.sl_order_id)
            log.info("trade_service: pre-sqoff cancel of SL %s for trade %s succeeded",
                     trade.sl_order_id, trade.id)
        except Exception as e:
            log.warning("trade_service: pre-sqoff cancel of SL %s failed (continuing): %s",
                        trade.sl_order_id, e)
        # Detach the SL locally so a trailing tick doesn't try to interact with
        # a now-stale order id at the broker.
        trade.sl_order_id = None
    trade.lifecycle_stage = LIFECYCLE_EXITING
    order_id = broker.place_order(
        OrderRequest(
            instrument_key=trade.option_instrument_key,
            transaction_type="SELL",
            quantity=trade.quantity,
            product=PRODUCT,
            order_type="MARKET",
            tag=f"trade-{trade.id}",
        )
    )
    exit_price = derive_exit_price(broker, trade, sqoff_id=order_id)
    record_order(
        session, order_id=order_id, trade_id=trade.id, order_type="MARKET", transaction_type="SELL",
        instrument_token=trade.option_instrument_key, quantity=trade.quantity, tag=f"trade-{trade.id}",
        average_price=exit_price, tradingsymbol=trade.tradingsymbol,
    )
    close_trade(session, trade, exit_price=exit_price, exit_reason=reason, closure_cause=closure_cause)
    log.info("trade_service: square-off completed trade %s | exit=%.2f pnl=%.2f",
             trade.id, exit_price, trade.realized_pnl)
    return exit_price


def set_lifecycle_stage(trade: Trade, stage: str) -> None:
    """Transition the trade's lifecycle stage. Idempotent — caller decides when."""
    trade.lifecycle_stage = stage


def sync_order_status(session, broker, order_id: str) -> OrderView | None:
    """Pull the broker view for `order_id` and update our `Order` row in place.

    Returns the live OrderView (or None if unknown) so the caller can branch on
    status. Keeps `status`, `status_message`, `average_price`, `filled_quantity`,
    `order_timestamp`, and `exchange_timestamp` in sync. Idempotent; safe to call
    every tracker tick.

    If our row says `status_message` was set by manual intervention, we still
    overwrite — the broker is the source of truth.
    """
    try:
        book = broker.get_order_book()
    except Exception as e:
        log.warning("trade_service: get_order_book for sync failed: %s", e)
        return None
    view = next((o for o in book if o.order_id == order_id), None)
    if view is None:
        return None
    order = session.get(Order, order_id)
    if order is None:
        return view
    if order.status != view.status:
        log.info("trade_service: order %s status %s -> %s", order_id, order.status, view.status)
    order.status = view.status
    order.status_message = view.status_message
    if view.average_price is not None:
        order.average_price = view.average_price
    order.filled_quantity = int(view.filled_quantity or 0)
    if view.order_timestamp is not None:
        order.order_timestamp = view.order_timestamp
    if view.exchange_timestamp is not None:
        order.exchange_timestamp = view.exchange_timestamp
    order.trigger_price = view.trigger_price if view.trigger_price is not None else order.trigger_price
    order.price = float(view.price or 0.0)
    return view


def collect_orders_for_trade(broker, trade: Trade) -> list[OrderView]:
    """Return broker order_views for `trade.option_instrument_key` SELL orders.

    Intentionally NOT filtered by tag — we want to see user-placed orders too
    (tag won't match ours, but the instrument does). The bot orders are
    identified by `order_id == trade.sl_order_id`; everything else is treated
    as external (user-placed).
    """
    try:
        book = broker.get_order_book()
    except Exception as e:
        log.warning("trade_service: get_order_book for collect failed: %s", e)
        return []
    return [
        o for o in book
        if o.instrument_token == trade.option_instrument_key
        and o.transaction_type == "SELL"
    ]


def adopt_external_sl(trade: Trade, view: OrderView) -> bool:
    """If `view` looks like a TIGHTER open SELL SL order placed by the user,
    adopt its `trigger_price` as the new `current_sl` (ratchet only).

    For an option-buying bot, "tighter" means a HIGHER trigger_price (closer
    to current LTP). The bot never loosens — if the user-set trigger is
    below our current_sl, we keep the higher floor (our ratchet value).
    Returns True iff we tightened/adopted, False if the trigger is no tighter
    than current or unavailable. Updates `trade.sl_source` so the UI can
    show the user that their manual change is in effect.
    """
    if view.status != "open":
        return False
    if view.order_id == trade.sl_order_id:
        return False  # ours; the trail loop modifies it directly
    if view.trigger_price is None:
        return False
    new_sl = float(view.trigger_price)
    if new_sl <= float(trade.current_sl or 0.0):
        return False  # user proposed a looser SL — keep our floor
    log.info(
        "trade_service: adopting external SL order %s trigger=%.2f for trade %s "
        "(previous current_sl=%.2f, sl_source=%s)",
        view.order_id, new_sl, trade.id, trade.current_sl, trade.sl_source,
    )
    trade.current_sl = new_sl
    trade.sl_source = "user"
    return True


def drift_broker_sl(trade: Trade, view: OrderView) -> bool:
    """Sync bot-placed SL when the broker rounds-trips its trigger_price back
    different from `trade.current_sl`. Only tighten (ratchet up).

    Called after each `modify_order` to confirm the broker accepted the new
    trigger and our local view matches. Returns True if we adjusted.
    """
    if view.order_id != trade.sl_order_id:
        return False
    if view.trigger_price is None:
        return False
    broker_trigger = float(view.trigger_price)
    if abs(broker_trigger - float(trade.current_sl or 0.0)) < 1e-6:
        return False
    if broker_trigger > float(trade.current_sl or 0.0):
        # Broker reports a TIGHTER trigger than we asked for — adopt it.
        log.info(
            "trade_service: bot SL drift on trade %s: broker=%.2f > local=%.2f (tightening to broker)",
            trade.id, broker_trigger, trade.current_sl,
        )
        trade.current_sl = broker_trigger
        return True
    # Broker reports a LOOSER trigger than we asked for — keep our ratchet
    # value so we don't widen the protective stop.
    log.info(
        "trade_service: broker SL trigger (%.2f) is looser than trade.current_sl (%.2f) for trade %s; keeping local",
        broker_trigger, trade.current_sl, trade.id,
    )
    return False


def initial_sl_for(entry_price: float, direction: str, sl_pct: float,
                   instrument_tick: float = 0.0) -> float:
    """Initial SL trigger price for a CALL/PUT option.

    The bot always BUYs options, so both CE and PE positions carry the same
    downside risk: the premium falling below entry. The protective SL must
    therefore be BELOW entry for both directions (price * (1 - sl_pct/100)).

    The trigger is snapped to the option's NSE tick band (0.05/0.10/0.50) so
    Upstox doesn't reject the order for an off-tick price.
    """
    raw = entry_price * (1.0 - sl_pct / 100.0)
    tick = option_tick_for(raw, instrument_tick)
    snapped = round_to_tick(raw, tick)
    if snapped >= entry_price:
        snapped = round_to_tick(entry_price - tick, tick)
    return snapped


def avg_fill_price(fills) -> float | None:
    if not fills:
        return None
    total_qty = sum(f.quantity for f in fills) or 1
    return sum(f.average_price * f.quantity for f in fills) / total_qty


def compute_trailing_sl(trade: Trade, ltp: float, activate_pct: float, gap_pct: float,
                         instrument_tick: float = 0.0) -> tuple[float, str]:
    """Trailing SL rule (v2 — activate above initial SL, then track ltp ± gap%).

    Phase 1 (at_initial): SL stays at the initial SL. The rule activates once
    the LTP moves `activate_pct`% beyond the initial SL in the profitable
    direction. For a CALL with initial SL = 90 and activate_pct = 20, that
    means ltp >= 90 * 1.20 = 108. Symmetric for PUTs.

    Phase 2 (trailing): each tick the candidate SL is `ltp * (1 - gap_pct/100)`
    for CALL / `ltp * (1 + gap_pct/100)` for PUT, snapped to the option tick
    band. The SL only ever moves in the profitable direction (ratchet):
    monotonic-up for CALL, monotonic-down for PUT. Once `trail_state` is
    `"trailing"`, the activation gate is no longer re-applied — the rule
    stays in trailing mode for the lifetime of the trade.

    Returns `(new_sl, new_trail_state)`. `new_sl` is `trade.current_sl` when
    the rule has nothing to update, so callers can skip the modify call.
    """
    initial = trade.initial_sl
    already_trailing = trade.trail_state == "trailing"

    if trade.direction == "CALL":
        activation_ltp = initial * (1.0 + activate_pct / 100.0)
        if not already_trailing and ltp < activation_ltp:
            return trade.current_sl, "at_initial"
        candidate = ltp * (1.0 - gap_pct / 100.0)
        tick = option_tick_for(candidate, instrument_tick)
        snapped = round_to_tick(candidate, tick)
        cap = round_to_tick(ltp - tick, tick)
        if snapped >= cap:
            snapped = max(cap - tick, tick)
        if not already_trailing:
            return snapped, "trailing"
        return max(snapped, trade.current_sl), "trailing"

    # PUT: the bot is a buyer, so PUT risk is the premium falling — same as
    # CALL. Profitable direction is LTP rising, so we activate when ltp climbs
    # `activate_pct`% above the initial SL and trail `gap_pct`% below LTP,
    # ratcheting UP only (locks profit as the PUT premium rises).
    activation_ltp = initial * (1.0 + activate_pct / 100.0)
    if not already_trailing and ltp < activation_ltp:
        return trade.current_sl, "at_initial"
    candidate = ltp * (1.0 - gap_pct / 100.0)
    tick = option_tick_for(candidate, instrument_tick)
    snapped = round_to_tick(candidate, tick)
    cap = round_to_tick(ltp - tick, tick)
    if snapped >= cap:
        snapped = max(cap - tick, tick)
    if not already_trailing:
        return snapped, "trailing"
    return max(snapped, trade.current_sl), "trailing"


def is_sl_hit(trade: Trade, ltp: float) -> bool:
    return ltp <= trade.current_sl