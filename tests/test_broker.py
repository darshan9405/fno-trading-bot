"""Broker layer tests: SDK-call mapping and normalization (no network)."""

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest
import upstox_client
from upstox_client.rest import ApiException

from app.broker.base import BrokerError, ModifyOrderParams, OrderRequest
from app.broker.upstox_broker import (
    UpstoxBroker,
    parse_candles,
    to_fill_view,
    to_order_view,
    to_position_view,
)
from app.config import Config


def _broker(token="test-token"):
    return UpstoxBroker(Config(), access_token=token)


# --- pure helpers ---------------------------------------------------------


def test_parse_candles_builds_dataframe():
    raw = [
        ["2026-09-04T09:15:00+05:30", 26800.0, 26850.0, 26790.0, 26830.0, 120000, 340000],
        ["2026-09-04T09:16:00+05:30", 26830.0, 26840.0, 26780.0, 26790.0, 80000, 341000],
    ]
    df = parse_candles(raw)
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "oi"]
    assert len(df) == 2
    assert df.loc[df.index[0], "close"] == 26830.0
    assert df.index[0].tz is not None  # parsed with offset


def test_parse_candles_empty():
    df = parse_candles([])
    assert df.empty
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "oi"]


def test_to_position_view_maps_fields():
    p = SimpleNamespace(
        exchange="NSE", product="D", instrument_token="NSE_FO|84123", tradingsymbol="NIFTY 10 SEP 26 26800 CE",
        quantity=50, average_price=245.0, last_price=273.8, multiplier=50.0,
        unrealised=1440.0, realised=0.0, pnl=1440.0,
    )
    v = to_position_view(p)
    assert v.instrument_token == "NSE_FO|84123"
    assert v.quantity == 50
    assert v.multiplier == 50.0
    assert v.unrealised == 1440.0


def test_to_order_view_and_fill_view():
    o = SimpleNamespace(
        order_id="o-1", exchange_order_id="11000", status="complete", status_message="OK",
        order_type="MARKET", variety="regular", transaction_type="BUY", product="D",
        price=0.0, trigger_price=0.0, average_price=245.0, quantity=50, filled_quantity=50,
        instrument_token="NSE_FO|84123", tradingsymbol="NIFTY 10 SEP 26 26800 CE", exchange="NSE",
        tag="trade-201",
    )
    v = to_order_view(o)
    assert v.order_id == "o-1"
    assert v.tag == "trade-201"
    assert v.average_price == 245.0

    f = SimpleNamespace(trade_id="t-1", order_id="o-1", average_price=245.0, quantity=50, transaction_type="BUY")
    fv = to_fill_view(f)
    assert fv.trade_id == "t-1"
    assert fv.average_price == 245.0


# --- SDK-call mapping -----------------------------------------------------


def test_get_historical_candles_calls_sdk(monkeypatch):
    broker = _broker()
    calls = {}

    class FakeHistory:
        def get_historical_candle_data1(self, instrument_key, interval, to_date, from_date, api_version):
            calls.update(instrument_key=instrument_key, interval=interval, to_date=to_date, from_date=from_date, api_version=api_version)
            return SimpleNamespace(data=SimpleNamespace(candles=[
                ["2026-09-04T09:15:00+05:30", 1, 2, 0.5, 1.5, 100, 1000],
            ]))

    broker._apis["history"] = FakeHistory()
    df = broker.get_historical_candles("NSE_INDEX|Nifty 50", "day", date(2026, 9, 1), date(2026, 9, 4))
    assert calls == {
        "instrument_key": "NSE_INDEX|Nifty 50", "interval": "day",
        "to_date": "2026-09-04", "from_date": "2026-09-01", "api_version": "2.0",
    }
    assert len(df) == 1


def test_get_historical_candles_rejects_bad_interval():
    broker = _broker()
    with pytest.raises(BrokerError, match="Unsupported interval"):
        broker.get_historical_candles("NSE_INDEX|Nifty 50", "nanosecond", date(2026, 9, 1), date(2026, 9, 4))


def test_get_ltp_keys_by_instrument_token(monkeypatch):
    broker = _broker()
    fake = SimpleNamespace(ltp=lambda symbol, api_version: SimpleNamespace(
        data={
            "NSE_EQ:NHPC": SimpleNamespace(last_price=52.05, instrument_token="NSE_EQ|INE848E01016"),
            "NSE_EQ:MISSING": SimpleNamespace(last_price=None, instrument_token="NSE_EQ|INE669E01016"),
        }
    ))
    broker._apis["quote"] = fake
    result = broker.get_ltp(["NSE_EQ|INE848E01016", "NSE_EQ|INE669E01016"])
    assert result == {"NSE_EQ|INE848E01016": 52.05}


def test_get_ltp_falls_back_to_response_key(monkeypatch):
    broker = _broker()
    fake = SimpleNamespace(ltp=lambda symbol, api_version: SimpleNamespace(
        data={"NSE_FO|1": SimpleNamespace(last_price=100.5)}
    ))
    broker._apis["quote"] = fake
    result = broker.get_ltp(["NSE_FO|1"])
    assert result == {"NSE_FO|1": 100.5}


def test_get_positions(monkeypatch):
    broker = _broker()
    fake = SimpleNamespace(get_positions=lambda api_version: SimpleNamespace(data=[
        SimpleNamespace(instrument_token="NSE_FO|84123", tradingsymbol="X", quantity=50, average_price=245.0,
                        last_price=273.8, multiplier=50.0, unrealised=1440.0, realised=0.0, pnl=1440.0)
    ]))
    broker._apis["portfolio"] = fake
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].unrealised == 1440.0


def test_get_funds(monkeypatch):
    broker = _broker()
    fake = SimpleNamespace(get_user_fund_margin=lambda api_version: SimpleNamespace(data={
        "equity": SimpleNamespace(available_margin=184500.0, used_margin=12800.0, span_margin=12000.0,
                                  exposure_margin=800.0, notional_cash=5000.0)
    }))
    broker._apis["user"] = fake
    funds = broker.get_funds()
    assert funds.available_margin == 184500.0
    assert funds.span_margin == 12000.0


def test_place_order_builds_v3_request(monkeypatch):
    broker = _broker()
    received = {}

    class FakeOrderV3:
        def place_order(self, body):
            received["body"] = body
            return SimpleNamespace(data=SimpleNamespace(order_ids=["o-22014"]))

    broker._apis["order_v3"] = FakeOrderV3()
    order_id = broker.place_order(OrderRequest(
        instrument_key="NSE_FO|84123", transaction_type="BUY", quantity=50,
        product="D", order_type="LIMIT", price=101.0, tag="trade-201",
    ))
    assert order_id == "o-22014"
    body = received["body"]
    assert isinstance(body, upstox_client.PlaceOrderV3Request)
    assert body.instrument_token == "NSE_FO|84123"
    assert body.transaction_type == "BUY"
    assert body.quantity == 50
    assert body.product == "D"
    assert body.tag == "trade-201"
    assert body.order_type == "LIMIT"
    assert body.price == 101.0
    assert body.market_protection == -1  # Upstox sentinel for standard guidelines


def test_modify_order_sets_trigger_price(monkeypatch):
    broker = _broker()
    received = {}

    class FakeOrderV3:
        def modify_order(self, body):
            received["body"] = body
            return SimpleNamespace(status="success")

    broker._apis["order_v3"] = FakeOrderV3()
    broker.modify_order(ModifyOrderParams(
        order_id="o-22017", quantity=50, trigger_price=273.6, order_type="SL", price=273.6, validity="DAY",
    ))
    body = received["body"]
    assert isinstance(body, upstox_client.ModifyOrderRequest)
    assert body.order_id == "o-22017"
    assert body.trigger_price == 273.6
    # NSE rejects SL-M for options (NSE/FAOP/49677); bot uses SL with limit=trigger.
    assert body.order_type == "SL"
    assert body.price == 273.6
    assert body.quantity == 50
    assert body.validity == "DAY"
    assert body.market_protection == -1


def test_exit_all_passes_segment(monkeypatch):
    broker = _broker()
    received = {}

    class FakeOrderV2:
        def exit_positions(self, **kwargs):
            received.update(kwargs)
            return SimpleNamespace(status="success")

    broker._apis["order_v2"] = FakeOrderV2()
    broker.exit_all(segment="NSE_FO")
    assert received == {"segment": "NSE_FO"}


def test_get_order_book_and_fills(monkeypatch):
    broker = _broker()
    broker._apis["order_v2"] = SimpleNamespace(
        get_order_book=lambda api_version: SimpleNamespace(data=[SimpleNamespace(order_id="o-1", status="complete")]),
        get_trades_by_order=lambda order_id, api_version: SimpleNamespace(data=[SimpleNamespace(trade_id="t-1", order_id="o-1")]),
    )
    orders = broker.get_order_book()
    assert orders[0].order_id == "o-1"
    fills = broker.get_trades_by_order("o-1")
    assert fills[0].trade_id == "t-1"


def test_get_expiries_parses_dates(monkeypatch):
    broker = _broker()
    fake = SimpleNamespace(
        get_option_contracts=lambda instrument_key, **kwargs: SimpleNamespace(data=[
            SimpleNamespace(instrument_key="K1", trading_symbol="T1", instrument_type="CE",
                            lot_size=50, strike_price=26800.0, tick_size=0.0,
                            expiry="2026-09-10"),
            SimpleNamespace(instrument_key="K2", trading_symbol="T2", instrument_type="PE",
                            lot_size=50, strike_price=26800.0, tick_size=0.0,
                            expiry="2026-09-17"),
            SimpleNamespace(instrument_key="K3", trading_symbol="T3", instrument_type="CE",
                            lot_size=50, strike_price=26800.0, tick_size=0.0,
                            expiry=None),
        ])
    )
    broker._apis["options"] = fake
    expiries = broker.get_expiries("NSE_INDEX|Nifty 50")
    assert expiries == [date(2026, 9, 10), date(2026, 9, 17)]


def test_get_expiries_raises_when_empty(monkeypatch):
    broker = _broker()
    fake = SimpleNamespace(get_option_contracts=lambda instrument_key, **kwargs: SimpleNamespace(data=[]))
    broker._apis["options"] = fake
    with pytest.raises(BrokerError):
        broker.get_expiries("NSE_INDEX|Nifty 50")


def test_get_option_contracts_passes_expiry(monkeypatch):
    broker = _broker()
    received = {}

    class FakeOptions:
        def get_option_contracts(self, instrument_key, **kwargs):
            received.update(instrument_key=instrument_key, **kwargs)
            return SimpleNamespace(data=[
                SimpleNamespace(instrument_key="NSE_FO|84123", instrument_type="CE", lot_size=50,
                                strike_price=26800.0, expiry="2026-09-10T00:00:00+05:30",
                                underlying_key="NSE_INDEX|Nifty 50", trading_symbol="NIFTY 10 SEP 26 26800 CE")
            ])

    broker._apis["options"] = FakeOptions()
    contracts = broker.get_option_contracts("NSE_INDEX|Nifty 50", expiry=date(2026, 9, 10))
    assert received["instrument_key"] == "NSE_INDEX|Nifty 50"
    assert received["expiry_date"] == "2026-09-10"
    assert contracts[0].instrument_key == "NSE_FO|84123"
    assert contracts[0].lot_size == 50
    assert contracts[0].strike_price == 26800.0


def test_get_profile(monkeypatch):
    broker = _broker()
    fake = SimpleNamespace(get_profile=lambda api_version: SimpleNamespace(
        data=SimpleNamespace(user_id="usr-1", user_name="Trader", email="a@b.com", broker="UPSTOX")
    ))
    broker._apis["user"] = fake
    profile = broker.get_profile()
    assert profile.user_id == "usr-1"
    assert profile.broker == "UPSTOX"


# --- error handling -------------------------------------------------------


def test_api_exception_wraps_to_broker_error(monkeypatch):
    broker = _broker()

    class FakeQuote:
        def ltp(self, symbol, api_version):
            raise ApiException(status=500, reason="Internal Server Error")

    broker._apis["quote"] = FakeQuote()
    with pytest.raises(BrokerError) as excinfo:
        broker.get_ltp(["NSE_FO|1"])
    assert excinfo.value.api_status == 500
    assert "get_ltp failed" in str(excinfo.value)


def test_missing_token_raises():
    broker = _broker(token="")
    with pytest.raises(BrokerError, match="access token not set"):
        broker.get_positions()


def test_set_access_token_rebuilds_client():
    broker = _broker(token="old")
    broker.set_access_token("new")
    assert broker._token == "new"
    assert broker._apis == {}
    assert broker._api_client is not None


# --- market calendar -----------------------------------------------------


def test_get_market_holidays(monkeypatch):
    broker = _broker()
    fake = SimpleNamespace(get_holidays=lambda: SimpleNamespace(data=[
        SimpleNamespace(_date="2026-09-04T00:00:00+05:30", description="Ganesh Chaturthi",
                        holiday_type="NSE", closed_exchanges=["NSE", "BSE"], open_exchanges=[]),
    ]))
    broker._apis["market_calendar"] = fake
    holidays = broker.get_market_holidays()
    assert len(holidays) == 1
    assert holidays[0].date == date(2026, 9, 4)
    assert holidays[0].description == "Ganesh Chaturthi"
    assert holidays[0].closed_exchanges == ["NSE", "BSE"]


def test_get_exchange_timings(monkeypatch):
    broker = _broker()
    fake = SimpleNamespace(get_exchange_timings=lambda _date: SimpleNamespace(data=[
        SimpleNamespace(exchange="NSE", start_time=1000, end_time=1400),
    ]))
    broker._apis["market_calendar"] = fake
    timings = broker.get_exchange_timings(date(2026, 9, 4))
    assert len(timings) == 1
    assert timings[0].exchange == "NSE"
    assert timings[0].start_time == 1000