"""API integration tests (Stages 5-7): killswitch, trades, P&L, health, config."""

from datetime import date
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app import auth as auth_module
from app import create_app
from app.broker.base import (
    BrokerBase,
    FillView,
    FundsView,
    InstrumentView,
    PositionView,
    ProfileView,
)
from app.config import Config
from app.db import dispose, session_scope
from app.models import Trade
from app.settings import set_setting


class FakeLoginApi:
    def __init__(self, api_client=None):
        pass

    def token(self, api_version, **kwargs):
        return SimpleNamespace(access_token="upstox-token", user_id="usr-1", user_name="Trader")


class FakeBroker(BrokerBase):
    def __init__(self):
        self.placed = []
        self.ltp_map = {}
        self.positions = []
        self.funds = FundsView(available_margin=250000.0)
        self.fills = {}

    def get_historical_candles(self, instrument_key, interval, from_date, to_date):
        return None

    def get_ltp(self, instrument_keys):
        return {k: self.ltp_map.get(k, 100.0) for k in instrument_keys}

    def place_order(self, order):
        self.placed.append(order)
        oid = f"o-{len(self.placed)}"
        avg = self.ltp_map.get(order.instrument_key, 100.0)
        self.fills[oid] = [FillView(trade_id=f"t-{len(self.placed)}", order_id=oid,
                                    quantity=order.quantity, average_price=avg,
                                    transaction_type=order.transaction_type)]
        return oid

    def modify_order(self, params):
        pass

    def cancel_order(self, order_id):
        pass

    def exit_all(self, tag=None, segment=None):
        pass

    def get_positions(self):
        return self.positions

    def get_funds(self):
        return self.funds

    def get_order_book(self):
        return []

    def get_trades_by_order(self, order_id):
        return self.fills.get(order_id, [])

    def get_expiries(self, underlying_key):
        return [date(2026, 9, 10)]

    def get_option_contracts(self, underlying_key, expiry=None):
        return []

    def get_profile(self):
        return ProfileView(user_id="usr-1", broker="UPSTOX")

    def search_instruments(self, query):
        return []

    def get_market_holidays(self):
        return []

    def get_exchange_timings(self, day):
        return []


def _seed_open_trade(age_minutes=0):
    with session_scope() as session:
        trade = Trade(
            underlying_key="NSE_INDEX|Nifty 50",
            option_instrument_key="NSE_FO|84123",
            option_instrument_token="84123",
            tradingsymbol="NIFTY 10 SEP 26 26800 CE",
            lot_size=50, product="I", direction="CALL",
            entry_price=100.0, quantity=50, initial_sl=90.0, current_sl=90.0,
            trail_state="at_initial", best_price=100.0, status="open",
            entry_order_id="o-entry", sl_order_id="o-sl",
        )
        if age_minutes:
            from datetime import timedelta

            from app.services.health_service import utcnow

            trade.entry_time = utcnow() - timedelta(minutes=age_minutes)
        session.add(trade)
        session.flush()
        trade_id = trade.id
    return trade_id


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = Config()
    cfg.RATE_LIMIT_ENABLED = False
    cfg.DATABASE_URL = f"sqlite:///{tmp_path / 'api.db'}"
    cfg.SECRET_KEY = "test-secret"
    cfg.JWT_MASTER_SECRET = "test-master"
    cfg.JWT_ACCESS_TTL_MINUTES = 15
    cfg.JWT_REFRESH_TTL_DAYS = 7
    cfg.FRONTEND_URL = "http://localhost:8501"
    cfg.COOKIE_SECURE = False

    auth_module.configure(cfg)
    from app.auth import UpstoxTokenStore

    UpstoxTokenStore._token = None
    UpstoxTokenStore._loaded = False

    dispose()
    app = create_app(cfg)
    app.config["TESTING"] = True

    monkeypatch.setattr("upstox_client.LoginApi", FakeLoginApi)

    with app.test_client() as client:
        loc = client.get("/api/auth/upstox/callback?code=c").headers["Location"]
        boot = parse_qs(urlparse(loc).query)["bootstrap"][0]
        token = client.post("/api/auth/bootstrap", json={"code": boot}).get_json()["data"]["access_token"]
        yield client, token, cfg
    dispose()


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


# --- auth guard ----------------------------------------------------------


def test_protected_endpoints_require_jwt(env):
    client, token, cfg = env
    # drop the SSO cookies so the client is genuinely unauthenticated
    client.delete_cookie("upstox_at", path="/")
    client.delete_cookie("upstox_rt", path="/api/auth")
    assert client.get("/api/trades/open").status_code == 401
    assert client.post("/api/killswitch/activate").status_code == 401
    assert client.get("/api/health").status_code == 401
    assert client.get("/api/config").status_code == 401


# --- killswitch ----------------------------------------------------------


def test_killswitch_flow(env, monkeypatch):
    client, token, cfg = env
    trade_id = _seed_open_trade()
    fake = FakeBroker()
    monkeypatch.setattr("app.api.killswitch_api.get_broker", lambda config=None: fake)

    r = client.post("/api/killswitch/activate", json={"reason": "test kill"}, headers=_headers(token))
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["active"] is True
    assert data["squared_off"][0]["trade_id"] == trade_id

    with session_scope() as session:
        trade = session.get(Trade, trade_id)
        assert trade.status == "closed"
        assert trade.exit_reason == "killswitch"
        assert trade.realized_pnl == 0.0  # exited at 100 == entry

    assert client.get("/api/killswitch/status", headers=_headers(token)).get_json()["data"]["active"] is True
    r = client.post("/api/killswitch/release", headers=_headers(token))
    assert r.get_json()["data"]["active"] is False


class FailingBroker(FakeBroker):
    def __init__(self, fail_on_place=2):
        super().__init__()
        self.fail_on_place = fail_on_place

    def place_order(self, order):
        if len(self.placed) + 1 >= self.fail_on_place:
            from app.broker.base import BrokerError

            raise BrokerError("simulated square-off failure")
        return super().place_order(order)


def test_killswitch_partial_failure_sets_flag(env, monkeypatch):
    client, token, cfg = env
    t1, t2 = _seed_open_trade(), _seed_open_trade()
    fake = FailingBroker(fail_on_place=2)
    monkeypatch.setattr("app.api.killswitch_api.get_broker", lambda config=None: fake)

    r = client.post("/api/killswitch/activate", json={"reason": "partial"}, headers=_headers(token))
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["active"] is True  # flag set regardless of square-off failure
    squared_ids = {s["trade_id"] for s in data["squared_off"]}
    failed_ids = {f["trade_id"] for f in data["failed"]}
    assert len(squared_ids) == 1 and len(failed_ids) == 1
    assert squared_ids | failed_ids == {t1, t2}

    with session_scope() as session:
        for trade in session.execute(select(Trade)).scalars():
            assert (trade.status == "closed") == (trade.id in squared_ids)  # isolation: other trade untouched

    assert client.get("/api/killswitch/status", headers=_headers(token)).get_json()["data"]["active"] is True


def test_recon_closes_orphan_trade(env, monkeypatch):
    client, token, cfg = env
    trade_id = _seed_open_trade(age_minutes=10)  # old enough to reconcile
    fake = FakeBroker()
    fake.positions = []  # broker no longer holds it
    monkeypatch.setattr("app.api.trade_api.get_broker", lambda config=None: fake)

    r = client.post("/api/trades/recon", headers=_headers(token))
    assert r.status_code == 200
    assert r.get_json()["data"]["reconciled"][0]["trade_id"] == trade_id
    with session_scope() as session:
        t = session.get(Trade, trade_id)
        assert t.status == "closed"
        assert t.exit_reason == "recon"


def test_recon_keeps_held_trade(env, monkeypatch):
    client, token, cfg = env
    trade_id = _seed_open_trade(age_minutes=10)
    fake = FakeBroker()
    fake.positions = [PositionView(instrument_token="84123", tradingsymbol="X", quantity=50)]
    monkeypatch.setattr("app.api.trade_api.get_broker", lambda config=None: fake)

    client.post("/api/trades/recon", headers=_headers(token))
    with session_scope() as session:
        assert session.get(Trade, trade_id).status == "open"


def test_instruments_list_and_toggle(env):
    client, token, cfg = env
    from app.models import Instrument

    with session_scope() as session:
        session.add_all([
            Instrument(symbol="NIFTY", exchange="NSE", segment="NSE_INDEX",
                       spot_instrument_key="NSE_INDEX|Nifty 50", instrument_token="26000",
                       trading_symbol="NIFTY", lot_size=50, enabled=False),
            Instrument(symbol="RELIANCE", exchange="NSE", segment="NSE_EQ",
                       spot_instrument_key="NSE_EQ|RELIANCE", instrument_token="2885",
                       trading_symbol="RELIANCE", lot_size=1250, enabled=True),
        ])

    # Auth required.
    client.delete_cookie("upstox_at", path="/")
    client.delete_cookie("upstox_rt", path="/api/auth")
    assert client.get("/api/instruments").status_code == 401

    r = client.get("/api/instruments", headers=_headers(token))
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["count"] == 2
    by = {i["symbol"]: i for i in data["instruments"]}
    assert by["NIFTY"]["enabled"] is False
    assert by["RELIANCE"]["enabled"] is True

    r = client.put(f"/api/instruments/{by['NIFTY']['id']}", json={"enabled": True}, headers=_headers(token))
    assert r.status_code == 200
    assert r.get_json()["data"]["enabled"] is True

    # Invalid body / unknown id.
    assert client.put(f"/api/instruments/{by['NIFTY']['id']}", json={"enabled": "yes"},
                      headers=_headers(token)).status_code == 400
    assert client.put("/api/instruments/99999", json={"enabled": True},
                      headers=_headers(token)).status_code == 404

    with session_scope() as session:
        by_id = {i.symbol: i for i in session.execute(select(Instrument)).scalars()}
        assert by_id["NIFTY"].enabled is True  # toggled on
        assert by_id["RELIANCE"].enabled is True  # unchanged (was already on)


# --- trades + P&L --------------------------------------------------------


def test_trades_open_closed_and_pnl(env, monkeypatch):
    client, token, cfg = env
    trade_id = _seed_open_trade()

    fake = FakeBroker()
    fake.ltp_map["NSE_FO|84123"] = 105.0
    fake.positions = [PositionView(instrument_token="NSE_FO|84123", tradingsymbol="NIFTY 10 SEP 26 26800 CE",
                                   quantity=50, average_price=100.0, last_price=105.0,
                                   unrealised=250.0, realised=0.0, pnl=250.0, multiplier=50.0)]
    monkeypatch.setattr("app.api.trade_api.get_broker", lambda config=None: fake)

    r = client.get("/api/trades/open", headers=_headers(token))
    assert r.status_code == 200
    trades = r.get_json()["data"]["trades"]
    assert len(trades) == 1
    assert trades[0]["id"] == trade_id
    assert trades[0]["unrealised_pnl"] == 250.0  # (105 - 100) * 50

    r = client.get("/api/trades/pnl", headers=_headers(token))
    data = r.get_json()["data"]
    assert data["unrealised"] == 250.0
    assert data["available_margin"] == 250000.0
    assert data["total"] == 250.0

    assert client.get("/api/trades/closed", headers=_headers(token)).get_json()["data"]["trades"] == []


def test_pnl_requires_upstox_token(env):
    client, token, cfg = env
    r = client.get("/api/trades/pnl", headers=_headers(token))
    assert r.status_code == 502  # no Upstox token stored -> BrokerError


# --- health + config -----------------------------------------------------


def test_health_and_config(env, monkeypatch):
    client, token, cfg = env
    set_setting("sqoff_time", "15:30")
    monkeypatch.setattr("app.api.health_api.get_broker", lambda config=None: FakeBroker())

    r = client.get("/api/health", headers=_headers(token))
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert "heartbeats" in data and "market" in data and "errors" in data
    assert data["broker"]["configured"] is True
    assert data["broker"]["connected"] is True
    # Upstox token expiry surfaced (SSO in the fixture stored a token)
    assert "token_valid_until" in data["broker"]
    assert data["broker"]["token_expired"] is False

    r = client.get("/api/config", headers=_headers(token))
    cfg_data = r.get_json()["data"]
    assert cfg_data["sqoff_time"] == "15:30"
    assert "breakout.patterns_enabled" in cfg_data

    r = client.put("/api/config", json={"initial_sl_pct": 12.5}, headers=_headers(token))
    assert r.status_code == 200
    assert client.get("/api/config", headers=_headers(token)).get_json()["data"]["initial_sl_pct"] == 12.5

    r = client.put("/api/config", json={"not_a_setting": 1}, headers=_headers(token))
    assert r.status_code == 400