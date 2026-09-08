"""Trade persistence + trailing-SL math shared by order_placer and trade_tracker."""

from sqlalchemy import select

from app.broker.base import InstrumentView, OrderRequest
from app.db import session_scope
from app.models import Lead, Order, OrderFill, Trade
from app.services.health_service import utcnow
from app.settings import get_setting

PRODUCT = "D"  # delivery


def _token_from_key(instrument_key: str) -> str:
    return instrument_key.split("|")[-1]


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
                 average_price: float | None = None, trigger_price: float | None = None,
                 tradingsymbol: str | None = None) -> Order:
    order = Order(
        order_id=order_id,
        trade_id=trade_id,
        order_type=order_type,
        variety="regular",
        transaction_type=transaction_type,
        product=PRODUCT,
        price=0.0,
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
    order_id = broker.place_order(
        OrderRequest(
            instrument_key=trade.option_instrument_key,
            transaction_type="SELL",
            quantity=trade.quantity,
            product=PRODUCT,
            order_type="MARKET",
            tag=f"trade-{trade.id}",
            market_protection=int(get_setting("market_protection_pct", 2)),
        )
    )
    exit_price = avg_fill_price(broker.get_trades_by_order(order_id))
    if exit_price is None:
        exit_price = (broker.get_ltp([trade.option_instrument_key]) or {}).get(trade.option_instrument_key)
    exit_price = exit_price or trade.current_sl
    record_order(
        session, order_id=order_id, trade_id=trade.id, order_type="MARKET", transaction_type="SELL",
        instrument_token=trade.option_instrument_key, quantity=trade.quantity, tag=f"trade-{trade.id}",
        average_price=exit_price, tradingsymbol=trade.tradingsymbol,
    )
    close_trade(session, trade, exit_price=exit_price, exit_reason=reason)
    return exit_price


def initial_sl_for(entry_price: float, direction: str, sl_pct: float) -> float:
    factor = (1.0 - sl_pct / 100.0) if direction == "CALL" else (1.0 + sl_pct / 100.0)
    return round(entry_price * factor, 2)


def avg_fill_price(fills) -> float | None:
    if not fills:
        return None
    total_qty = sum(f.quantity for f in fills) or 1
    return sum(f.average_price * f.quantity for f in fills) / total_qty


def compute_trailing_sl(trade: Trade, ltp: float, activate_pct: float, gap_pct: float) -> tuple[float, str]:
    """Return (new_sl, new_trail_state) per the trailing rule.

    breakeven once favourable move >= activate_pct; then trail `gap_pct` from best_price.
    """
    entry, best = trade.entry_price, trade.best_price
    if trade.direction == "CALL":
        fav = (best - entry) / entry
    else:
        fav = (entry - best) / entry

    new_sl = trade.current_sl
    state = trade.trail_state
    if fav >= activate_pct / 100.0:
        if state == "at_initial":
            new_sl, state = entry, "breakeven"
        else:
            if trade.direction == "CALL":
                candidate = best * (1.0 - gap_pct / 100.0)
                if candidate > new_sl:
                    new_sl, state = round(candidate, 2), "trailing"
            else:
                candidate = best * (1.0 + gap_pct / 100.0)
                if candidate < new_sl:
                    new_sl, state = round(candidate, 2), "trailing"
    return new_sl, state


def is_sl_hit(trade: Trade, ltp: float) -> bool:
    if trade.direction == "CALL":
        return ltp <= trade.current_sl
    return ltp >= trade.current_sl