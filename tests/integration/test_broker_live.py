"""Live broker smoke suite for the EC2 deployment (marked `integration`).

Reads the Upstox credentials/token as configured in the environment:

* `.env` is loaded by `app.config.Config` (UPSTOX_API_BASE, UPSTOX_ORDER_BASE,
  UPSTOX_API_VERSION, ...).
* The access token is the SSO-obtained token stored in the DB (see
  `app.auth.UpstoxTokenStore`), or an explicit `UPSTOX_ACCESS_TOKEN` override.

Read-only (never places/cancels orders). Run on EC2 as a post-deploy sanity
check, e.g.:

    pytest -m integration tests/integration/test_broker_live.py -v

Skips when no token is available.
"""

import os
from datetime import date, timedelta

import pytest

from app.broker import get_broker
from app.broker.base import BrokerError
from app.broker.upstox_broker import UpstoxBroker
from app.config import Config

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def broker():
    try:
        token = os.getenv("UPSTOX_ACCESS_TOKEN")
        if token:
            b = UpstoxBroker(Config(), access_token=token)
        else:
            b = get_broker(Config())
    except Exception as e:
        pytest.skip(f"could not load broker token: {e}")
    if not b._token:
        pytest.skip("no Upstox access token: complete SSO, or set UPSTOX_ACCESS_TOKEN")
    return b


def probe(fn):
    try:
        return fn(), None
    except BrokerError as e:
        return None, e


def eq_key(broker) -> str:
    matches = [i for i in broker.search_instruments("RELIANCE") if i.segment == "NSE_EQ" and i.instrument_key]
    assert matches, "no NSE_EQ instrument found for RELIANCE"
    return matches[0].instrument_key


# --- auth / market data ---------------------------------------------------


def test_broker_has_token(broker):
    assert broker._token


def test_search_instruments(broker):
    res = broker.search_instruments("NIFTY")
    assert res
    assert all(i.instrument_key and i.trading_symbol for i in res)


def test_ltp_keys_match_request_for_eq_and_index(broker):
    keys = ["NSE_INDEX|Nifty 50", eq_key(broker)]
    res, err = probe(lambda: broker.get_ltp(keys))
    assert err is None, f"ltp failed: {err}"
    assert set(keys) <= set(res), f"get_ltp must return the requested keys, got {sorted(res)}"
    assert all(v > 0 for v in res.values())


def test_historical_candles(broker):
    to_date = date.today() - timedelta(days=1)
    res, err = probe(lambda: broker.get_historical_candles(
        "NSE_INDEX|Nifty 50", "day", to_date - timedelta(days=60), to_date
    ))
    assert err is None, f"candles failed: {err}"
    assert res is not None and not res.empty
    assert list(res.columns) == ["open", "high", "low", "close", "volume", "oi"]


def test_expiries_and_option_contracts(broker):
    expiries, err = probe(lambda: broker.get_expiries("NSE_INDEX|Nifty 50"))
    assert err is None, f"expiries failed: {err}"
    assert expiries
    contracts, c_err = probe(lambda: broker.get_option_contracts("NSE_INDEX|Nifty 50", expiry=expiries[0]))
    assert c_err is None, f"option contracts failed: {c_err}"
    assert contracts
    assert all(c.instrument_key and c.lot_size for c in contracts)


def test_option_ltp_keys_match_request(broker):
    expiries = broker.get_expiries("NSE_INDEX|Nifty 50")
    contracts = broker.get_option_contracts("NSE_INDEX|Nifty 50", expiry=expiries[0])
    assert contracts
    keys = [c.instrument_key for c in contracts[:2]]
    res, err = probe(lambda: broker.get_ltp(keys))
    assert err is None, f"option ltp failed: {err}"
    assert set(keys) <= set(res), f"option get_ltp must return the requested keys, got {sorted(res)}"


# --- portfolio ------------------------------------------------------------


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


# --- market calendar ------------------------------------------------------


def test_market_holidays(broker):
    res, err = probe(broker.get_market_holidays)
    assert err is None, f"market holidays failed: {err}"
    assert res is not None


def test_exchange_timings(broker):
    res, err = probe(lambda: broker.get_exchange_timings(date.today()))
    assert err is None, f"exchange timings failed: {err}"
    assert isinstance(res, list)