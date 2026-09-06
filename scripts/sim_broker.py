"""Test-only simulated broker for the pre-market dry-run harness.

Implements BrokerBase but never touches the real Upstox API: serves injected
candles, fills orders at a scripted LTP, and records SL/trail modifications.
Lives under scripts/ and is NOT wired into get_broker (production always uses
UpstoxBroker).
"""

from datetime import date

from app.broker.base import (
    BrokerBase,
    FillView,
    FundsView,
    InstrumentView,
    OrderView,
    ProfileView,
)


class SimBroker(BrokerBase):
    def __init__(self, candles: dict[str, object] | None = None,
                 contracts: list[InstrumentView] | None = None,
                 expiries: list[date] | None = None):
        self.candles = candles or {}
        self.contracts = contracts or []
        self.expiries = expiries or []
        self.ltp_map: dict[str, float] = {}
        self.placed: list[OrderView] = []
        self.modified: list[object] = []
        self.fills: dict[str, list[FillView]] = {}
        self._seq = 0

    def _oid(self) -> str:
        self._seq += 1
        return f"sim-{self._seq}"

    def set_ltps(self, prices: dict[str, float]) -> None:
        self.ltp_map.update(prices)

    # --- market data ------------------------------------------------------

    def get_historical_candles(self, instrument_key, interval, from_date, to_date):
        return self.candles.get(instrument_key)

    def get_ltp(self, instrument_keys):
        return {k: self.ltp_map.get(k, 100.0) for k in instrument_keys}

    # --- orders -----------------------------------------------------------

    def place_order(self, order):
        oid = self._oid()
        price = self.ltp_map.get(order.instrument_key, 100.0)
        if order.order_type == "SL-M":
            self.placed.append(OrderView(order_id=oid, order_type="SL-M",
                                         transaction_type=order.transaction_type,
                                         quantity=order.quantity, trigger_price=order.trigger_price,
                                         instrument_token=order.instrument_key, status="pending"))
        else:
            self.fills[oid] = [FillView(trade_id=f"f-{oid}", order_id=oid,
                                        quantity=order.quantity, average_price=price,
                                        transaction_type=order.transaction_type)]
            self.placed.append(OrderView(order_id=oid, order_type=order.order_type,
                                         transaction_type=order.transaction_type,
                                         quantity=order.quantity, average_price=price,
                                         instrument_token=order.instrument_key, status="complete"))
        return oid

    def modify_order(self, params):
        self.modified.append(params)

    def cancel_order(self, order_id):
        for o in self.placed:
            if o.order_id == order_id:
                o.status = "cancelled"

    def exit_all(self, tag=None, segment=None):
        pass

    def get_order_book(self):
        return self.placed

    def get_trades_by_order(self, order_id):
        return self.fills.get(order_id, [])

    # --- portfolio / options ---------------------------------------------

    def get_positions(self):
        return []

    def get_funds(self):
        return FundsView(available_margin=500000.0)

    def get_expiries(self, underlying_key):
        return self.expiries

    def get_option_contracts(self, underlying_key, expiry=None):
        return [c for c in self.contracts if c.underlying_key == underlying_key]

    def get_profile(self):
        return ProfileView(user_id="sim", broker="SIM")

    def search_instruments(self, query):
        return []

    def get_market_holidays(self):
        return []

    def get_exchange_timings(self, day):
        return []