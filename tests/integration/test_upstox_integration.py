"""Live Upstox integration tests against the production API (marked `integration`, opt-in).

Run with:
    UPSTOX_INTEGRATION_TOKEN=<token> UPSTOX_INTEGRATION=1 pytest -m integration tests/integration

The token is passed explicitly via UPSTOX_INTEGRATION_TOKEN (Upstox access
tokens expire, so tests never read a stored one). The order-lifecycle test
places a REAL order and only runs when UPSTOX_LIVE_ORDER=1 is set.
"""

import os
from datetime import date, timedelta

import pytest

from app.broker import UpstoxBroker
from app.broker.base import BrokerError, ModifyOrderParams, OrderRequest
from app.config import Config

pytestmark = pytest.mark.integration

if not os.getenv("UPSTOX_INTEGRATION"):
    pytest.skip("set UPSTOX_INTEGRATION=1 to run Upstox integration tests", allow_module_level=True)

TOKEN = os.getenv("UPSTOX_INTEGRATION_TOKEN")
if not TOKEN:
    pytest.skip("set UPSTOX_INTEGRATION_TOKEN to run Upstox integration tests", allow_module_level=True)


@pytest.fixture(scope="module")
def broker():
    return UpstoxBroker(Config(), access_token=TOKEN)


def probe(fn):
    try:
        return fn(), None
    except BrokerError as e:
        return None, e


# --- auth / config -------------------------------------------------------


def test_broker_has_token(broker):
    assert broker._token


def test_bad_token_raises_401():
    bad = UpstoxBroker(Config(), access_token="garbage-token")
    with pytest.raises(BrokerError) as excinfo:
        bad.search_instruments("NIFTY")
    assert excinfo.value.api_status == 401


# --- instruments / market data -------------------------------------------


def test_search_instruments(broker):
    res = broker.search_instruments("NIFTY")
    assert res
    assert all(i.instrument_key and i.trading_symbol for i in res)


def test_historical_candles(broker):
    to_date = date.today() - timedelta(days=1)  # only closed candles
    res, err = probe(lambda: broker.get_historical_candles("NSE_INDEX|Nifty 50", "day", to_date - timedelta(days=60), to_date))
    assert err is None, f"candles failed: {err}"
    assert res is not None and not res.empty
    assert list(res.columns) == ["open", "high", "low", "close", "volume", "oi"]


def test_ltp(broker):
    res, err = probe(lambda: broker.get_ltp(["NSE_INDEX|Nifty 50"]))
    assert err is None, f"ltp failed: {err}"
    assert res and any(v for v in res.values())


def test_expiries_and_option_contracts(broker):
    res, err = probe(lambda: broker.get_expiries("NSE_INDEX|Nifty 50"))
    assert err is None, f"expiries failed: {err}"
    assert res
    contracts, c_err = probe(lambda: broker.get_option_contracts("NSE_INDEX|Nifty 50", expiry=res[0]))
    assert c_err is None, f"option contracts failed: {c_err}"
    assert contracts
    assert all(c.instrument_key and c.lot_size for c in contracts)


# --- portfolio -----------------------------------------------------------


@pytest.mark.parametrize(
    "name,fn",
    [
        ("positions", lambda b: b.get_positions()),
        ("funds", lambda b: b.get_funds()),
        ("profile", lambda b: b.get_profile()),
    ],
)
def test_portfolio_endpoints(broker, name, fn):
    res, err = probe(lambda: fn(broker))
    assert err is None, f"{name} failed: {err}"
    assert res is not None


def test_order_book(broker):
    res, err = probe(broker.get_order_book)
    assert err is None, f"order book failed: {err}"
    assert isinstance(res, list)


# --- order lifecycle (REAL ORDER — opt-in) -------------------------------


def test_order_lifecycle(broker):
    if os.getenv("UPSTOX_LIVE_ORDER", "0") != "1":
        pytest.skip("this places a REAL order; set UPSTOX_LIVE_ORDER=1 to run")

    contracts = [i for i in broker.search_instruments("NIFTY 25000 CE") if i.instrument_type == "CE"]
    assert contracts
    c = contracts[0]
    qty = c.lot_size or 1

    order_id = broker.place_order(
        OrderRequest(
            instrument_key=c.instrument_key,
            transaction_type="BUY",
            quantity=qty,
            product="I",
            order_type="LIMIT",
            price=1.0,
            tag="integration-test",
        )
    )
    assert order_id

    book = broker.get_order_book()
    assert any(o.order_id == order_id for o in book), "placed order not found in order book"

    broker.modify_order(
        ModifyOrderParams(order_id=order_id, quantity=qty, price=2.0, order_type="LIMIT", trigger_price=0.0)
    )

    broker.cancel_order(order_id)
    statuses = {o.order_id: o.status for o in broker.get_order_book()}
    assert statuses.get(order_id, "cancelled") == "cancelled"