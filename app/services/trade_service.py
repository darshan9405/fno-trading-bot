"""Trade persistence + trailing-SL math shared by order_placer and trade_tracker."""

import logging

from sqlalchemy import select

from app.broker.base import BrokerError, InstrumentView, OrderRequest
from app.db import session_scope
from app.models import Lead, Order, OrderFill, Trade
from app.services.health_service import utcnow

log = logging.getLogger(__name__)

PRODUCT = "D"  # delivery / NRML for F&O (Upstox product code for carry-forward)

DEFAULT_OPTION_TICK = 0.05


def _token_from_key(instrument_key: str) -> str:
    return instrument_key.split("|")[-1]


def option_tick_for(price: float, instrument_tick: float = 0.0) -> float:
    """NSE option tick band: 0.05 below ₹250, 0.10 from ₹250–1000, 0.50 above.
    `instrument_tick` is the broker-reported tick; non-zero overrides the band."""
    if instrument_tick and instrument_tick > 0:
        return float(instrument_tick)
    if price >= 1000:
        return 0.50
    if price >= 250:
        return 0.10
    return 0.05


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
    tick band at the trigger price."""
    return round(max(trigger - tick, 0.05), 2)


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
    sl_limit = sl_price_below_trigger(trigger, option_tick_for(trigger, instrument_tick))
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


def close_trade(session, trade: Trade, *, exit_price: float, exit_reason: str) -> None:
    trade.status = "closed"
    trade.exit_time = utcnow()
    trade.exit_price = exit_price
    trade.exit_reason = exit_reason
    trade.realized_pnl = (exit_price - trade.entry_price) * trade.quantity


def square_off(session, broker, trade: Trade, reason: str = "sqoff") -> float:
    """Exit an open trade with a market SELL, record the order, close the trade."""
    log.info("trade_service: defensive square-off trade %s | reason=%s qty=%s price=%.2f",
             trade.id, reason, trade.quantity, trade.entry_price)
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
    close_trade(session, trade, exit_price=exit_price, exit_reason=reason)
    log.info("trade_service: square-off completed trade %s | exit=%.2f pnl=%.2f",
             trade.id, exit_price, trade.realized_pnl)
    return exit_price


def initial_sl_for(entry_price: float, direction: str, sl_pct: float,
                   instrument_tick: float = 0.0) -> float:
    """Initial SL trigger price for a CALL/PUT option.

    CALL: SL below entry (price * (1 - sl_pct/100))
    PUT : SL above entry (price * (1 + sl_pct/100))

    The trigger is snapped to the option's NSE tick band (0.05/0.10/0.50) so
    Upstox doesn't reject the order for an off-tick price.
    """
    factor = (1.0 - sl_pct / 100.0) if direction == "CALL" else (1.0 + sl_pct / 100.0)
    raw = entry_price * factor
    tick = option_tick_for(raw, instrument_tick)
    snapped = round_to_tick(raw, tick)
    if direction == "CALL" and snapped >= entry_price:
        snapped = round_to_tick(entry_price - tick, tick)
    elif direction == "PUT" and snapped <= entry_price:
        snapped = round_to_tick(entry_price + tick, tick)
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

    # PUT
    activation_ltp = initial * (1.0 - activate_pct / 100.0)
    if not already_trailing and ltp > activation_ltp:
        return trade.current_sl, "at_initial"
    candidate = ltp * (1.0 + gap_pct / 100.0)
    tick = option_tick_for(candidate, instrument_tick)
    snapped = round_to_tick(candidate, tick)
    floor = round_to_tick(ltp + tick, tick)
    if snapped <= floor:
        snapped = floor + tick
    if not already_trailing:
        return snapped, "trailing"
    return min(snapped, trade.current_sl), "trailing"


def is_sl_hit(trade: Trade, ltp: float) -> bool:
    if trade.direction == "CALL":
        return ltp <= trade.current_sl
    return ltp >= trade.current_sl