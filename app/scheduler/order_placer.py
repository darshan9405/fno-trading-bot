"""Scheduler 3 — Order Placer.

Picks queued leads and turns them into trades:
validate (killswitch / no open trade / price divergence / option expiry >= N days)
-> resolve the option contract (ATM strike, correct CE/PE) -> place entry (market)
-> place initial SL (SL-M) -> persist trade + order audit.
"""

import logging
import traceback

from sqlalchemy import select

from app.broker import get_broker
from app.broker.base import BrokerError, OrderRequest
from app.config import Config
from app.db import session_scope
from app.models import Lead
from app.services import contract_service, health_service, market_calendar, trade_service
from app.services.killswitch_service import is_killswitch_active
from app.services.lead_service import mark_lead
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
        market_protection = int(get_setting("market_protection_pct", 2))

        available_margin = None
        if margin_check:
            try:
                available_margin = getattr(broker.get_funds(), "available_margin", None)
            except Exception as e:
                log.warning("order_placer: margin fetch failed (%s); margin check skipped", e)

        pending_errors = []
        with session_scope() as session:
            # Consume highest-confidence leads first so the best signals get filled
            # before lower-conviction ones when margin/limits constrain how many trade.
            leads = list(session.execute(
                select(Lead).where(Lead.status == "queued").order_by(Lead.confidence.desc(), Lead.created_at)
            ).scalars())
            for lead in leads:
                try:
                    process_lead(session, broker, lead, sl_pct, max_div, min_days, lots, available_margin, max_depth,
                                 market_protection)
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
                 available_margin: float | None = None, max_depth: int = 3, market_protection: int = 0) -> None:
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

    expiry = contract_service.next_expiry(broker, lead.underlying_key, min_days)
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

    entry_order_id = broker.place_order(
        OrderRequest(
            instrument_key=contract.instrument_key,
            transaction_type="BUY",
            quantity=quantity,
            product="I",
            order_type="MARKET",
            tag=tag,
            market_protection=market_protection,
        )
    )

    entry_price = trade_service.avg_fill_price(broker.get_trades_by_order(entry_order_id))
    if entry_price is None:
        entry_price = (broker.get_ltp([contract.instrument_key]) or {}).get(contract.instrument_key)
    if entry_price is None:
        raise BrokerError(f"could not determine entry price for {contract.instrument_key}")

    initial_sl = trade_service.initial_sl_for(entry_price, lead.direction, sl_pct)

    sl_order_id = broker.place_order(
        OrderRequest(
            instrument_key=contract.instrument_key,
            transaction_type="SELL",
            quantity=quantity,
            product="I",
            order_type="SL-M",
            trigger_price=initial_sl,
            tag=tag,
            market_protection=market_protection,
        )
    )

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
        session, order_id=entry_order_id, trade_id=trade.id, order_type="MARKET", transaction_type="BUY",
        instrument_token=contract.instrument_key, quantity=quantity, tag=tag, average_price=entry_price,
        tradingsymbol=contract.trading_symbol,
    )
    trade_service.record_order(
        session, order_id=sl_order_id, trade_id=trade.id, order_type="SL-M", transaction_type="SELL",
        instrument_token=contract.instrument_key, quantity=quantity, tag=tag, trigger_price=initial_sl,
        tradingsymbol=contract.trading_symbol,
    )
    mark_lead(session, lead, "placed", note=f"trade={trade.id}")

    log.info("order_placer: opened trade %s for %s entry=%.2f sl=%.2f", trade.id, lead.underlying_key, entry_price, initial_sl)