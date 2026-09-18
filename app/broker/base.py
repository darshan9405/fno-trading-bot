"""Broker abstraction: neutral types + BrokerBase ABC.

Schedulers/services/UI depend only on these types; the Upstox SDK mapping
lives in upstox_broker.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

FNO_OPTION_TICK = 0.05  # NSE F&O options: uniform ₹0.05 tick across all strikes/premiums.


def normalize_instrument_tick(raw) -> float:
    """Coerce a broker-reported `tick_size` to a sane rupee value.

    Upstox option-contract responses historically return `tick_size` as integer
    paise (5 → ₹0.05, 10 → ₹0.10, 50 → ₹0.50). Treat values >=1 that equal an
    integer as paise and divide by 100; consume floats directly. Out-of-range
    values (≤0 or >100) fall back to `FNO_OPTION_TICK` so a bad contract never
    distorts the SL or entry price.
    """
    if raw is None:
        return FNO_OPTION_TICK
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return FNO_OPTION_TICK
    if v <= 0:
        return FNO_OPTION_TICK
    if v >= 1 and v == int(v):
        v = v / 100.0
    if v > 100:
        return FNO_OPTION_TICK
    return round(v, 4)


class BrokerError(Exception):
    """Raised when a broker operation fails (wraps SDK ApiException)."""

    def __init__(self, message, *, api_status=None, api_message=None, order_id=None):
        super().__init__(message)
        self.message = message
        self.api_status = api_status
        self.api_message = api_message
        self.order_id = order_id


@dataclass
class OrderRequest:
    instrument_key: str  # NSE_FO|...
    transaction_type: str  # BUY | SELL
    quantity: int
    product: str = "D"  # delivery (NRML for F&O)
    order_type: str = "MARKET"  # MARKET | LIMIT | SL | SL-M
    price: float = 0.0
    trigger_price: float = 0.0
    tag: str | None = None
    validity: str = "DAY"
    is_amo: bool = False


@dataclass
class ModifyOrderParams:
    """Full spec for modifying an order (v3 ModifyOrderRequest requires all).

    The bot prefers `SL-M` (Stop-Loss-Market, no limit price) and falls back
    to `SL` with `price < trigger_price` (Upstox UDAPI1038) when the broker
    rejects SL-M for the segment. Modify keeps the original order_type, so
    pass the same one you used on placement.
    """

    order_id: str
    quantity: int
    trigger_price: float
    order_type: str = "SL-M"
    price: float = 0.0
    validity: str = "DAY"
    disclosed_quantity: int = 0


@dataclass
class PositionView:
    instrument_token: str = ""
    tradingsymbol: str = ""
    exchange: str = "NSE"
    product: str = ""
    quantity: int = 0
    average_price: float = 0.0
    last_price: float = 0.0
    multiplier: float = 1.0
    unrealised: float = 0.0
    realised: float = 0.0
    pnl: float = 0.0


@dataclass
class FundsView:
    available_margin: float = 0.0
    used_margin: float = 0.0
    span_margin: float = 0.0
    exposure_margin: float = 0.0
    notional_cash: float = 0.0


@dataclass
class OrderView:
    order_id: str = ""
    exchange_order_id: str | None = None
    status: str = ""
    status_message: str | None = None
    order_type: str = ""
    variety: str = ""
    transaction_type: str = ""
    product: str = ""
    price: float = 0.0
    trigger_price: float | None = None
    average_price: float | None = None
    quantity: int = 0
    filled_quantity: int = 0
    instrument_token: str = ""
    tradingsymbol: str | None = None
    exchange: str = "NSE"
    tag: str | None = None
    order_timestamp: datetime | None = None
    exchange_timestamp: datetime | None = None


@dataclass
class FillView:
    trade_id: str = ""
    order_id: str = ""
    exchange_order_id: str | None = None
    instrument_token: str = ""
    tradingsymbol: str | None = None
    transaction_type: str = ""
    quantity: int = 0
    average_price: float = 0.0
    exchange_timestamp: datetime | None = None


@dataclass
class InstrumentView:
    """Instrument metadata (whitelist building / option contracts)."""

    instrument_key: str = ""
    trading_symbol: str = ""
    name: str = ""
    exchange: str = ""
    segment: str = ""
    instrument_type: str = ""  # EQ | FUT | CE | PE | ...
    expiry: date | None = None
    strike_price: float | None = None
    lot_size: int = 1
    tick_size: float = 0.0
    underlying_key: str | None = None


@dataclass
class ProfileView:
    user_id: str = ""
    user_name: str = ""
    email: str = ""
    broker: str = ""


@dataclass
class ExchangeTimingView:
    """Raw per-exchange session timing for a date (start/end as given by the SDK)."""

    exchange: str = ""
    start_time: int | None = None
    end_time: int | None = None


@dataclass
class HolidayView:
    """Market holiday / special-session day (NSE closed or open with timings)."""

    date: date | None = None
    description: str = ""
    holiday_type: str = ""
    closed_exchanges: list[str] = field(default_factory=list)
    open_exchanges: list[ExchangeTimingView] = field(default_factory=list)


class BrokerBase(ABC):
    """Neutral broker interface. Implementations wrap a broker SDK."""

    @abstractmethod
    def get_historical_candles(self, instrument_key: str, interval: str, from_date: date, to_date: date) -> pd.DataFrame:
        """OHLCV daily/intraday candles. Returns DataFrame indexed by timestamp
        with columns open/high/low/close/volume/oi."""

    @abstractmethod
    def get_ltp(self, instrument_keys: list[str]) -> dict[str, float]:
        """Map instrument_key -> last traded price."""

    @abstractmethod
    def get_positions(self) -> list[PositionView]:
        """Open positions (authoritative source for live P&L)."""

    @abstractmethod
    def get_funds(self) -> FundsView:
        """Funds & margin snapshot."""

    @abstractmethod
    def place_order(self, order: OrderRequest) -> str:
        """Place an order; returns the broker order_id."""

    @abstractmethod
    def modify_order(self, params: ModifyOrderParams) -> None:
        """Modify an open order (used for trailing stop-loss moves)."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> None:
        """Cancel an open order."""

    @abstractmethod
    def exit_all(self, tag: str | None = None, segment: str | None = None) -> None:
        """Exit positions filtered by order tag and/or segment."""

    @abstractmethod
    def get_order_book(self) -> list[OrderView]:
        """Current order book."""

    @abstractmethod
    def get_trades_by_order(self, order_id: str) -> list[FillView]:
        """Fills for a given order."""

    @abstractmethod
    def get_expiries(self, underlying_key: str) -> list[date]:
        """Available expiry dates for an underlying (>= 5-day rule)."""

    @abstractmethod
    def get_option_contracts(self, underlying_key: str, expiry: date | None = None) -> list[InstrumentView]:
        """Option contracts (strikes/lot size/keys) for an underlying."""

    @abstractmethod
    def get_profile(self) -> ProfileView:
        """Logged-in user profile."""

    @abstractmethod
    def search_instruments(self, query: str) -> list[InstrumentView]:
        """Free-text instrument search (whitelist builder)."""

    @abstractmethod
    def get_market_holidays(self) -> list[HolidayView]:
        """Market holidays / special-session days for the current year."""

    @abstractmethod
    def get_exchange_timings(self, day: date) -> list[ExchangeTimingView]:
        """Per-exchange session timings for a given date."""