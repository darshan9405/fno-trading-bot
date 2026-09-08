"""Upstox broker implementation wrapping the official upstox_client SDK.

Maps BrokerBase methods to the verified SDK surface (HistoryApi, MarketQuoteApi,
PortfolioApi, UserApi, OrderApi, OrderApiV3, OptionsApi, InstrumentsApi).
No mock — this is the real integration.
"""

import logging
from datetime import date

import pandas as pd
import upstox_client
from upstox_client.rest import ApiException

from app.broker.base import (
    BrokerBase,
    BrokerError,
    ExchangeTimingView,
    FillView,
    FundsView,
    HolidayView,
    InstrumentView,
    ModifyOrderParams,
    OrderRequest,
    OrderView,
    PositionView,
    ProfileView,
)
from app.config import Config

log = logging.getLogger(__name__)

API_VERSION = "2.0"

INTERVALS = {"1minute", "3minute", "5minute", "10minute", "15minute", "30minute", "60minute", "day", "week", "month"}

CANDLE_COLUMNS = ["open", "high", "low", "close", "volume", "oi"]


def _iso(d: date) -> str:
    return d.isoformat()


def _dt(value) -> object | None:
    if not value:
        return None
    try:
        return pd.to_datetime(value).to_pydatetime()
    except (ValueError, TypeError):
        return None


def _dt_date(value) -> date | None:
    dt = _dt(value)
    return dt.date() if dt else None


def parse_candles(candles) -> pd.DataFrame:
    """Raw SDK candles `[[ts,o,h,l,c,v,oi], ...]` -> DataFrame indexed by timestamp."""
    if not candles:
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    df = pd.DataFrame(candles, columns=["timestamp"] + CANDLE_COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    for col in CANDLE_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.set_index("timestamp").sort_index()


def to_position_view(p) -> PositionView:
    return PositionView(
        instrument_token=getattr(p, "instrument_token", "") or "",
        tradingsymbol=getattr(p, "tradingsymbol", "") or getattr(p, "trading_symbol", "") or "",
        exchange=getattr(p, "exchange", "") or "NSE",
        product=getattr(p, "product", "") or "",
        quantity=int(getattr(p, "quantity", 0) or 0),
        average_price=float(getattr(p, "average_price", 0.0) or 0.0),
        last_price=float(getattr(p, "last_price", 0.0) or 0.0),
        multiplier=float(getattr(p, "multiplier", 1.0) or 1.0),
        unrealised=float(getattr(p, "unrealised", 0.0) or 0.0),
        realised=float(getattr(p, "realised", 0.0) or 0.0),
        pnl=float(getattr(p, "pnl", 0.0) or 0.0),
    )


def to_order_view(o) -> OrderView:
    return OrderView(
        order_id=getattr(o, "order_id", "") or "",
        exchange_order_id=getattr(o, "exchange_order_id", None),
        status=getattr(o, "status", "") or "",
        status_message=getattr(o, "status_message", None),
        order_type=getattr(o, "order_type", "") or "",
        variety=getattr(o, "variety", "") or "regular",
        transaction_type=getattr(o, "transaction_type", "") or "",
        product=getattr(o, "product", "") or "",
        price=float(getattr(o, "price", 0.0) or 0.0),
        trigger_price=getattr(o, "trigger_price", None),
        average_price=getattr(o, "average_price", None),
        quantity=int(getattr(o, "quantity", 0) or 0),
        filled_quantity=int(getattr(o, "filled_quantity", 0) or 0),
        instrument_token=getattr(o, "instrument_token", "") or "",
        tradingsymbol=getattr(o, "tradingsymbol", None) or getattr(o, "trading_symbol", None),
        exchange=getattr(o, "exchange", "") or "NSE",
        tag=getattr(o, "tag", None),
        order_timestamp=_dt(getattr(o, "order_timestamp", None)),
        exchange_timestamp=_dt(getattr(o, "exchange_timestamp", None)),
    )


def to_fill_view(f) -> FillView:
    return FillView(
        trade_id=getattr(f, "trade_id", "") or "",
        order_id=getattr(f, "order_id", "") or "",
        exchange_order_id=getattr(f, "exchange_order_id", None),
        instrument_token=getattr(f, "instrument_token", "") or "",
        tradingsymbol=getattr(f, "tradingsymbol", None) or getattr(f, "trading_symbol", None),
        transaction_type=getattr(f, "transaction_type", "") or "",
        quantity=int(getattr(f, "quantity", 0) or 0),
        average_price=float(getattr(f, "average_price", 0.0) or 0.0),
        exchange_timestamp=_dt(getattr(f, "exchange_timestamp", None)),
    )


def _get(item, key, default=None):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def to_instrument_view(i) -> InstrumentView:
    return InstrumentView(
        instrument_key=_get(i, "instrument_key", "") or "",
        trading_symbol=_get(i, "trading_symbol", "") or _get(i, "name", "") or "",
        name=_get(i, "name", "") or "",
        exchange=_get(i, "exchange", "") or "",
        segment=_get(i, "segment", "") or "",
        instrument_type=_get(i, "instrument_type", "") or "",
        expiry=_dt_date(_get(i, "expiry", None)),
        strike_price=_get(i, "strike_price", None),
        lot_size=int(_get(i, "lot_size", 1) or 1),
        tick_size=float(_get(i, "tick_size", 0.0) or 0.0),
        underlying_key=_get(i, "underlying_key", None),
    )


def to_funds_view(data) -> FundsView:
    return FundsView(
        available_margin=float(getattr(data, "available_margin", 0.0) or 0.0),
        used_margin=float(getattr(data, "used_margin", 0.0) or 0.0),
        span_margin=float(getattr(data, "span_margin", 0.0) or 0.0),
        exposure_margin=float(getattr(data, "exposure_margin", 0.0) or 0.0),
        notional_cash=float(getattr(data, "notional_cash", 0.0) or 0.0),
    )


def make_configuration(config) -> upstox_client.Configuration:
    """SDK Configuration using the configured Upstox hosts (prod or sandbox)."""
    configuration = upstox_client.Configuration(sandbox=False)
    configuration.host = config.UPSTOX_API_BASE
    configuration.order_host = config.UPSTOX_ORDER_BASE
    return configuration


class UpstoxBroker(BrokerBase):
    def __init__(self, config: Config | None = None, access_token: str | None = None):
        self.config = config or Config()
        self._token = access_token or ""
        self._apis: dict[str, object] = {}
        self._rebuild_client()

    def _rebuild_client(self) -> None:
        configuration = make_configuration(self.config)
        configuration.access_token = self._token
        self._api_client = upstox_client.ApiClient(configuration)
        self._apis = {}

    def set_access_token(self, token: str) -> None:
        """Swap the bearer token (used by SSO / token refresh)."""
        self._token = token
        self._rebuild_client()

    def _require_token(self) -> None:
        if not self._token:
            raise BrokerError(
                "Upstox access token not set. Complete Upstox SSO to obtain one."
            )

    def _api(self, cls, name: str):
        if name not in self._apis:
            self._apis[name] = cls(self._api_client)
        return self._apis[name]

    def _to_broker_error(self, method: str, exc: ApiException) -> BrokerError:
        body = getattr(exc, "body", None)
        message = body if isinstance(body, str) else None
        if message is None and isinstance(body, dict):
            message = body.get("message") or body.get("error_description") or str(body)
        return BrokerError(
            f"{method} failed: {message or getattr(exc, 'reason', None) or exc}",
            api_status=getattr(exc, "status", None),
            api_message=message,
        )

    # --- market data ------------------------------------------------------

    def get_historical_candles(self, instrument_key: str, interval: str, from_date: date, to_date: date) -> pd.DataFrame:
        self._require_token()
        if interval not in INTERVALS:
            raise BrokerError(f"Unsupported interval {interval!r}. Use one of {sorted(INTERVALS)}")
        api = self._api(upstox_client.HistoryApi, "history")
        try:
            resp = api.get_historical_candle_data1(instrument_key, interval, _iso(to_date), _iso(from_date), API_VERSION)
        except ApiException as e:
            raise self._to_broker_error("get_historical_candles", e)
        data = getattr(resp, "data", None)
        return parse_candles(getattr(data, "candles", None) or [])

    def get_ltp(self, instrument_keys: list[str]) -> dict[str, float]:
        self._require_token()
        if not instrument_keys:
            return {}
        api = self._api(upstox_client.MarketQuoteApi, "quote")
        try:
            resp = api.ltp(",".join(instrument_keys), API_VERSION)
        except ApiException as e:
            raise self._to_broker_error("get_ltp", e)
        data = getattr(resp, "data", None) or {}
        out: dict[str, float] = {}
        for key, quote in data.items():
            if getattr(quote, "last_price", None) is None:
                continue
            out[getattr(quote, "instrument_token", None) or key] = float(quote.last_price)
        return out

    # --- portfolio --------------------------------------------------------

    def get_positions(self) -> list[PositionView]:
        self._require_token()
        api = self._api(upstox_client.PortfolioApi, "portfolio")
        try:
            resp = api.get_positions(API_VERSION)
        except ApiException as e:
            raise self._to_broker_error("get_positions", e)
        return [to_position_view(p) for p in (getattr(resp, "data", None) or [])]

    def get_funds(self) -> FundsView:
        self._require_token()
        api = self._api(upstox_client.UserApi, "user")
        try:
            resp = api.get_user_fund_margin(API_VERSION)
        except ApiException as e:
            raise self._to_broker_error("get_funds", e)
        data = getattr(resp, "data", None) or {}
        equity = data.get("equity") or next(iter(data.values()), None)
        return to_funds_view(equity) if equity is not None else FundsView()

    # --- orders -----------------------------------------------------------

    def place_order(self, order: OrderRequest) -> str:
        self._require_token()
        api = self._api(upstox_client.OrderApiV3, "order_v3")
        body = upstox_client.PlaceOrderV3Request(
            quantity=order.quantity,
            product=order.product,
            validity=order.validity,
            price=order.price,
            tag=order.tag,
            instrument_token=order.instrument_key,
            order_type=order.order_type,
            transaction_type=order.transaction_type,
            disclosed_quantity=0,
            trigger_price=order.trigger_price,
            is_amo=order.is_amo,
            market_protection=-1,
        )
        try:
            resp = api.place_order(body)
        except ApiException as e:
            raise self._to_broker_error("place_order", e)
        order_ids = getattr(getattr(resp, "data", None), "order_ids", None) or []
        if not order_ids:
            raise BrokerError("place_order: no order_id in response")
        return order_ids[0]

    def modify_order(self, params: ModifyOrderParams) -> None:
        self._require_token()
        api = self._api(upstox_client.OrderApiV3, "order_v3")
        body = upstox_client.ModifyOrderRequest(
            quantity=params.quantity,
            validity=params.validity,
            price=params.price,
            order_id=params.order_id,
            order_type=params.order_type,
            disclosed_quantity=params.disclosed_quantity,
            trigger_price=params.trigger_price,
            market_protection=-1,
        )
        try:
            api.modify_order(body)
        except ApiException as e:
            raise self._to_broker_error("modify_order", e)

    def cancel_order(self, order_id: str) -> None:
        self._require_token()
        api = self._api(upstox_client.OrderApiV3, "order_v3")
        try:
            api.cancel_order(order_id)
        except ApiException as e:
            raise self._to_broker_error("cancel_order", e)

    def exit_all(self, tag: str | None = None, segment: str | None = None) -> None:
        self._require_token()
        api = self._api(upstox_client.OrderApi, "order_v2")
        kwargs = {}
        if tag:
            kwargs["tag"] = tag
        if segment:
            kwargs["segment"] = segment
        try:
            api.exit_positions(**kwargs)
        except ApiException as e:
            raise self._to_broker_error("exit_all", e)

    def get_order_book(self) -> list[OrderView]:
        self._require_token()
        api = self._api(upstox_client.OrderApi, "order_v2")
        try:
            resp = api.get_order_book(API_VERSION)
        except ApiException as e:
            raise self._to_broker_error("get_order_book", e)
        return [to_order_view(o) for o in (getattr(resp, "data", None) or [])]

    def get_trades_by_order(self, order_id: str) -> list[FillView]:
        self._require_token()
        api = self._api(upstox_client.OrderApi, "order_v2")
        try:
            resp = api.get_trades_by_order(order_id, API_VERSION)
        except ApiException as e:
            raise self._to_broker_error("get_trades_by_order", e)
        return [to_fill_view(f) for f in (getattr(resp, "data", None) or [])]

    # --- options ----------------------------------------------------------

    def get_expiries(self, underlying_key: str) -> list[date]:
        """Live expiries for an underlying, derived from its option contracts.

        Upstox's `/v2/expired-instruments/expiries` endpoint returns past expiries
        only, which is the wrong source for live trading. The option contract
        universe is the canonical source of live future expiries.
        """
        self._require_token()
        contracts = self.get_option_contracts(underlying_key)
        expiries = {c.expiry for c in contracts if c.expiry is not None}
        if not expiries:
            raise BrokerError(f"no expiries available for {underlying_key}")
        return sorted(expiries)

    def get_option_contracts(self, underlying_key: str, expiry: date | None = None) -> list[InstrumentView]:
        self._require_token()
        api = self._api(upstox_client.OptionsApi, "options")
        kwargs = {"expiry_date": _iso(expiry)} if expiry else {}
        try:
            resp = api.get_option_contracts(underlying_key, **kwargs)
        except ApiException as e:
            raise self._to_broker_error("get_option_contracts", e)
        return [to_instrument_view(i) for i in (getattr(resp, "data", None) or [])]

    # --- account ----------------------------------------------------------

    def get_profile(self) -> ProfileView:
        self._require_token()
        api = self._api(upstox_client.UserApi, "user")
        try:
            resp = api.get_profile(API_VERSION)
        except ApiException as e:
            raise self._to_broker_error("get_profile", e)
        p = getattr(resp, "data", None)
        return ProfileView(
            user_id=getattr(p, "user_id", "") or "",
            user_name=getattr(p, "user_name", "") or "",
            email=getattr(p, "email", "") or "",
            broker=getattr(p, "broker", "") or "",
        )

    def search_instruments(self, query: str) -> list[InstrumentView]:
        self._require_token()
        api = self._api(upstox_client.InstrumentsApi, "instruments")
        try:
            resp = api.search_instrument(query)
        except ApiException as e:
            raise self._to_broker_error("search_instruments", e)
        return [to_instrument_view(i) for i in (getattr(resp, "data", None) or [])]

    # --- market calendar --------------------------------------------------

    def get_market_holidays(self) -> list[HolidayView]:
        self._require_token()
        api = self._api(upstox_client.MarketHolidaysAndTimingsApi, "market_calendar")
        try:
            resp = api.get_holidays()
        except ApiException as e:
            raise self._to_broker_error("get_market_holidays", e)
        out = []
        for h in (getattr(resp, "data", None) or []):
            open_timings = [
                ExchangeTimingView(
                    exchange=getattr(ex, "exchange", "") or "",
                    start_time=getattr(ex, "start_time", None),
                    end_time=getattr(ex, "end_time", None),
                )
                for ex in (getattr(h, "open_exchanges", None) or [])
            ]
            out.append(
                HolidayView(
                    date=_dt_date(getattr(h, "_date", None)) or _dt_date(getattr(h, "date", None)),
                    description=getattr(h, "description", "") or "",
                    holiday_type=getattr(h, "holiday_type", "") or "",
                    closed_exchanges=list(getattr(h, "closed_exchanges", None) or []),
                    open_exchanges=open_timings,
                )
            )
        return out

    def get_exchange_timings(self, day: date) -> list[ExchangeTimingView]:
        self._require_token()
        api = self._api(upstox_client.MarketHolidaysAndTimingsApi, "market_calendar")
        try:
            resp = api.get_exchange_timings(_iso(day))
        except ApiException as e:
            raise self._to_broker_error("get_exchange_timings", e)
        return [
            ExchangeTimingView(
                exchange=getattr(ex, "exchange", "") or "",
                start_time=getattr(ex, "start_time", None),
                end_time=getattr(ex, "end_time", None),
            )
            for ex in (getattr(resp, "data", None) or [])
        ]