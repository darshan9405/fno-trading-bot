"""API integration tests (Stages 5-7): killswitch, trades, P&L, health, config."""

import threading
from datetime import date, datetime, timezone
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

    def get_instruments(self):
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
            lot_size=50, product="D", direction="CALL",
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


def test_leads_generate_endpoint_dispatches(env, monkeypatch):
    """POST /leads/generate hands the work to a background job and returns 202
    with the job id so the UI can attach and poll for completion.
    """
    client, token, cfg = env
    from app.scheduler import lead_jobs

    fake_job = lead_jobs.JobState(
        id="abc123",
        status="running",
        submitted_at=datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(lead_jobs, "submit_manual_job", lambda: (fake_job, True))

    r = client.post("/api/trades/leads/generate", headers=_headers(token))
    assert r.status_code == 202
    body = r.get_json()
    assert body["data"]["id"] == "abc123"
    assert body["data"]["status"] == "running"


def test_leads_generate_endpoint_rejects_concurrent(env, monkeypatch):
    """A second POST while one run is in flight returns 409 with the existing
    job id so the UI can attach to the same status stream instead of
    dispatching a duplicate run."""
    client, token, cfg = env
    from app.scheduler import lead_jobs

    fake_job = lead_jobs.JobState(
        id="abc123",
        status="running",
        submitted_at=datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(lead_jobs, "submit_manual_job", lambda: (fake_job, False))

    r = client.post("/api/trades/leads/generate", headers=_headers(token))
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"]["code"] == "lead_generation_in_progress"
    assert body["data"]["id"] == "abc123"
    assert body["data"]["status"] == "running"


def test_leads_generate_status_unknown(env):
    client, token, cfg = env
    r = client.get("/api/trades/leads/generate/unknown-id", headers=_headers(token))
    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "lead_generation_job_not_found"


def test_leads_generate_status_terminal(env, monkeypatch):
    """End-to-end happy path: real job registry, broker call stubbed out, the
    background thread runs to completion and the status endpoint reflects the
    final result with the generated/checked counts."""
    import time

    client, token, cfg = env
    from app.scheduler import lead_jobs

    # The job registry is module-level; trim state from any earlier test.
    monkeypatch.setattr(lead_jobs, "_jobs", {})
    monkeypatch.setattr(lead_jobs, "_active_id", None)

    import app.scheduler.lead_generator as lg
    monkeypatch.setattr(
        lg, "run_lead_generator",
        lambda broker=None, now=None, force=False, on_progress=None: {"created": 5, "checked": 2},
    )

    r = client.post("/api/trades/leads/generate", headers=_headers(token))
    assert r.status_code == 202
    job_id = r.get_json()["data"]["id"]

    # Background thread is daemon=True; poll until terminal or timeout.
    deadline = time.monotonic() + 2.0
    status = None
    while time.monotonic() < deadline:
        rs = client.get(f"/api/trades/leads/generate/{job_id}", headers=_headers(token))
        assert rs.status_code == 200
        status = rs.get_json()["data"]["status"]
        if status in ("done", "error"):
            break
        time.sleep(0.05)
    assert status == "done"

    final = client.get(
        f"/api/trades/leads/generate/{job_id}", headers=_headers(token)
    ).get_json()["data"]
    assert final["result"] == {"generated": 5, "checked": 2}
    assert final["error"] is None
    assert final["finished_at"] is not None


def test_leads_generate_active_returns_404_when_nothing_in_flight(env):
    """The GET-active endpoint is the UI's lifeline after a page reload —
    if it 200s incorrectly when no manual job exists, the UI will poll a
    dead job id and silently 404 forever."""
    client, token, cfg = env
    from app.scheduler import lead_jobs

    monkeypatch_reset = {lead_jobs: {"_jobs": {}, "_active_id": None}}
    for mod, attrs in monkeypatch_reset.items():
        for k, v in attrs.items():
            setattr(mod, k, v)

    r = client.get("/api/trades/leads/generate/active", headers=_headers(token))
    assert r.status_code == 404
    assert r.get_json()["error"]["code"] == "lead_generation_no_active_job"


def test_leads_generate_active_returns_running_job(env, monkeypatch):
    """When a manual job is in flight, GET-active hands the UI the same
    job_id that POST /generate returned — completing the re-attach path."""
    import time

    client, token, cfg = env
    from app.scheduler import lead_jobs

    monkeypatch.setattr(lead_jobs, "_jobs", {})
    monkeypatch.setattr(lead_jobs, "_active_id", None)

    # Stub run_lead_generator so the daemon thread doesn't hit the real
    # broker. Sleep a tiny bit so we can observe the "running" state.
    started = threading.Event()

    def slow_run(broker=None, now=None, force=False, on_progress=None):
        started.set()
        time.sleep(0.5)
        return {"created": 0, "checked": 0}

    import app.scheduler.lead_generator as lg
    monkeypatch.setattr(lg, "run_lead_generator", slow_run)

    r = client.post("/api/trades/leads/generate", headers=_headers(token))
    assert r.status_code == 202
    job_id = r.get_json()["data"]["id"]

    r2 = client.get("/api/trades/leads/generate/active", headers=_headers(token))
    assert r2.status_code == 200
    assert r2.get_json()["data"]["id"] == job_id

    started.wait(timeout=2.0)


def test_leads_generate_status_includes_progress(env, monkeypatch):
    """The UI's progress panel relies on `progress` and `started_at` being
    present in the status payload. Verify both fields round-trip through
    the GET-status endpoint while the job is running."""
    import time

    client, token, cfg = env
    from app.scheduler import lead_jobs

    monkeypatch.setattr(lead_jobs, "_jobs", {})
    monkeypatch.setattr(lead_jobs, "_active_id", None)

    # Emit one progress patch, then complete — the test polls during the
    # brief window so we can observe the live payload.
    started = threading.Event()
    release = threading.Event()

    def run_with_progress(broker=None, now=None, force=False, on_progress=None):
        started.set()
        on_progress({
            "phase": "analyzing", "scanned": 1, "total": 4,
            "created": 1, "checked": 1, "errors": 0,
            "current": "FOO", "strategy": "test",
            "recent": [{"symbol": "FOO", "status": "leads",
                        "leads": 1, "error": None}],
        })
        release.wait(timeout=2.0)
        return {"created": 1, "checked": 1}

    import app.scheduler.lead_generator as lg
    monkeypatch.setattr(lg, "run_lead_generator", run_with_progress)

    r = client.post("/api/trades/leads/generate", headers=_headers(token))
    job_id = r.get_json()["data"]["id"]

    started.wait(timeout=2.0)
    rs = client.get(f"/api/trades/leads/generate/{job_id}", headers=_headers(token))
    assert rs.status_code == 200
    payload = rs.get_json()["data"]
    assert payload["status"] == "running"
    assert payload["progress"]["phase"] == "analyzing"
    assert payload["progress"]["total"] == 4
    assert payload["progress"]["recent"][0]["symbol"] == "FOO"
    assert payload["started_at"] is not None

    release.set()


def test_leads_include_fno_plan(env):
    client, token, cfg = env
    from app.models import Instrument, Lead

    with session_scope() as session:
        inst = Instrument(symbol="NIFTY", exchange="NSE", segment="NSE_INDEX",
                          spot_instrument_key="NSE_INDEX|Nifty 50", instrument_token="26000",
                          trading_symbol="NIFTY", lot_size=50, enabled=True)
        session.add(inst)
        session.flush()
        session.add(
            Lead(
                instrument_id=inst.id,
                underlying_key="NSE_INDEX|Nifty 50",
                direction="CALL",
                strategy="breakout",
                signal_type="horizontal_range",
                signal_level=26800.0,
                confidence=0.9,
                chart_interval="day",
                status="queued",
                plan={
                    "expiry": "2026-09-10",
                    "strike_price": 26800.0,
                    "option_type": "CE",
                    "trading_symbol": "NIFTY 10 SEP 26 26800 CE",
                    "lot_size": 50,
                    "quantity": 50,
                },
            )
        )

    r = client.get("/api/trades/leads", headers=_headers(token))
    assert r.status_code == 200
    leads = r.get_json()["data"]["leads"]
    assert len(leads) == 1
    row = leads[0]
    assert row["trading_symbol"] == "NIFTY 10 SEP 26 26800 CE"
    assert row["expiry"] == "2026-09-10"
    assert row["strike_price"] == 26800.0
    assert row["option_type"] == "CE"
    assert row["quantity"] == 50
    assert row["lot_size"] == 50
    # Tier-5: components exposed via the API for the breakdown UI.
    assert row["components"] == {}
    assert row["score_breakdown"] == []


def test_leads_include_components_breakdown(env):
    """Tier-5: when components are persisted on the Lead, the API must
    expose them as a `components` dict plus a flattened `score_breakdown`
    list consumable by the UI."""
    client, token, cfg = env
    from app.models import Instrument, Lead

    with session_scope() as session:
        inst = Instrument(symbol="NIFTY", exchange="NSE", segment="NSE_INDEX",
                          spot_instrument_key="NSE_INDEX|Nifty 50", instrument_token="26000",
                          trading_symbol="NIFTY", lot_size=50, enabled=True)
        session.add(inst)
        session.flush()
        session.add(
            Lead(
                instrument_id=inst.id,
                underlying_key="NSE_INDEX|Nifty 50",
                direction="CALL",
                strategy="breakout",
                signal_type="volume_breakout",
                signal_level=100.0,
                confidence=0.85,
                chart_interval="day",
                status="queued",
                components={
                    "pattern_fit":     0.85,
                    "volume":          0.95,
                    "trend_alignment": 0.5,
                    "proximity":       1.0,
                    "structure":       0.95,
                },
            )
        )

    r = client.get("/api/trades/leads", headers=_headers(token))
    assert r.status_code == 200
    lead = r.get_json()["data"]["leads"][0]
    assert lead["components"]["pattern_fit"] == pytest.approx(0.85)
    breakdown = {row["key"]: row for row in lead["score_breakdown"]}
    assert "pattern_fit" in breakdown
    assert "volume" in breakdown
    assert "trend_alignment" in breakdown
    assert "proximity" in breakdown
    # Weights + contributions add up across the listed dimensions.
    total_contrib = sum(row["contribution"] for row in lead["score_breakdown"])
    assert total_contrib == pytest.approx(0.85, abs=0.05)


def test_leads_endpoint_orders_by_score_desc(env):
    """The leads endpoint sorts by `confidence DESC, created_at DESC` by default."""
    client, token, cfg = env
    from app.models import Instrument, Lead
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        inst = Instrument(symbol="NIFTY", exchange="NSE", segment="NSE_INDEX",
                          spot_instrument_key="NSE_INDEX|Nifty 50", instrument_token="26000",
                          trading_symbol="NIFTY", lot_size=50, enabled=True)
        session.add(inst); session.flush()
        session.add_all([
            Lead(instrument_id=inst.id, underlying_key="NSE_INDEX|Nifty 50",
                 direction="CALL", strategy="breakout", signal_type="horizontal_range",
                 signal_level=100.0, confidence=0.5, status="queued",
                 created_at=base + timedelta(minutes=1)),
            Lead(instrument_id=inst.id, underlying_key="NSE_INDEX|Nifty 50",
                 direction="PUT", strategy="breakout", signal_type="triangle",
                 signal_level=100.0, confidence=0.9, status="queued",
                 created_at=base + timedelta(minutes=2)),
            Lead(instrument_id=inst.id, underlying_key="NSE_INDEX|Nifty 50",
                 direction="CALL", strategy="breakout", signal_type="trendline",
                 signal_level=100.0, confidence=0.7, status="queued",
                 created_at=base + timedelta(minutes=3)),
        ])

    # No params — the endpoint sorts by score by default.
    r = client.get("/api/trades/leads", headers=_headers(token))
    rows = r.get_json()["data"]["leads"]
    confidences = [row["confidence"] for row in rows]
    assert confidences == sorted(confidences, reverse=True)
    # Legacy filter params are silently accepted and ignored.
    r2 = client.get("/api/trades/leads?sort=score&date=2026-09-04&status=queued",
                    headers=_headers(token))
    assert r2.status_code == 200
    assert len(r2.get_json()["data"]["leads"]) == len(rows)


def test_delete_all_leads_requires_jwt(env):
    client, token, cfg = env
    client.delete_cookie("upstox_at", path="/")
    client.delete_cookie("upstox_rt", path="/api/auth")
    r = client.delete("/api/trades/leads")
    assert r.status_code == 401


def test_delete_all_leads_purges_rows_and_preserves_trades(env):
    """DELETE /api/trades/leads must remove every lead row regardless of
    status, null out Trade.lead_id on dependent trades, and leave Trade
    audit rows intact."""
    client, token, cfg = env
    from app.models import Instrument, Lead, Trade

    with session_scope() as session:
        inst = Instrument(symbol="NIFTY", exchange="NSE", segment="NSE_INDEX",
                          spot_instrument_key="NSE_INDEX|Nifty 50", instrument_token="26000",
                          trading_symbol="NIFTY", lot_size=50, enabled=True)
        session.add(inst)
        session.flush()
        # Two queued leads and one processed lead; a Trade is attached to the
        # processed one so we can assert FK nulling.
        queued_a = Lead(
            instrument_id=inst.id, underlying_key="NSE_INDEX|Nifty 50",
            direction="CALL", strategy="breakout", signal_type="horizontal_range",
            signal_level=100.0, confidence=0.7, status="queued",
        )
        queued_b = Lead(
            instrument_id=inst.id, underlying_key="NSE_INDEX|Nifty 50",
            direction="PUT", strategy="breakout", signal_type="trendline",
            signal_level=100.0, confidence=0.6, status="queued",
        )
        processed = Lead(
            instrument_id=inst.id, underlying_key="NSE_INDEX|Nifty 50",
            direction="CALL", strategy="breakout", signal_type="volume_breakout",
            signal_level=100.0, confidence=0.8, status="placed",
        )
        session.add_all([queued_a, queued_b, processed])
        session.flush()
        trade = Trade(
            lead_id=processed.id, underlying_key="NSE_INDEX|Nifty 50",
            option_instrument_key="NSE_FO|123", option_instrument_token="123",
            tradingsymbol="NIFTY 10 SEP 26 100 CE", lot_size=50,
            direction="CALL", entry_price=100.0, quantity=50,
            initial_sl=90.0, current_sl=90.0, trail_state="init",
            status="closed", entry_time=datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc),
            exit_time=datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc),
            exit_price=110.0, exit_reason="target", realized_pnl=500.0,
        )
        session.add(trade)
        session.flush()
        trade_id = trade.id

    r = client.delete("/api/trades/leads", headers=_headers(token))
    assert r.status_code == 200
    assert r.get_json()["data"]["deleted"] == 3

    with session_scope() as session:
        assert session.execute(select(Lead).limit(1)).scalars().all() == []
        surviving_trade = session.get(Trade, trade_id)
        assert surviving_trade is not None
        assert surviving_trade.lead_id is None
        assert surviving_trade.realized_pnl == 500.0

    # GET should now report zero rows.
    r = client.get("/api/trades/leads", headers=_headers(token))
    assert r.get_json()["data"]["count"] == 0


def test_delete_all_leads_when_empty(env):
    """Calling DELETE with no rows must be a no-op (returns deleted=0)."""
    client, token, cfg = env
    r = client.delete("/api/trades/leads", headers=_headers(token))
    assert r.status_code == 200
    assert r.get_json()["data"]["deleted"] == 0


# --- health + config -----------------------------------------------------


def test_health_and_config(env, monkeypatch):
    client, token, cfg = env
    set_setting("sqoff_time", "15:30")
    monkeypatch.setattr("app.api.health_api.get_broker", lambda config=None: FakeBroker())

    r = client.get("/api/health", headers=_headers(token))
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert "heartbeats" in data and "market" in data and "errors" in data
    assert "llm" in data  # LLM health block surfaced
    for hb in data["heartbeats"].values():  # UI reads hb["note"] on every row
        assert "note" in hb
    assert data["broker"]["configured"] is True
    assert data["broker"]["connected"] is True
    # Upstox token expiry surfaced (SSO in the fixture stored a token)
    assert "token_valid_until" in data["broker"]
    assert data["broker"]["token_expired"] is False

    # ISO 8601 contract: every timestamp the UI renders with ``_utc_to_ist_hm``
    # must be ``fromisoformat``-able. RFC 1123 (Flask default) silently turns
    # those rows into the ``—`` placeholder.
    assert datetime.fromisoformat(data["broker"]["token_valid_until"])
    for hb in data["heartbeats"].values():
        if hb["last_run_at"] is not None:
            assert datetime.fromisoformat(hb["last_run_at"])

    r = client.get("/api/config", headers=_headers(token))
    cfg_data = r.get_json()["data"]
    assert cfg_data["sqoff_time"] == "15:30"
    assert "llm.enabled" in cfg_data

    r = client.put("/api/config", json={"initial_sl_pct": 12.5}, headers=_headers(token))
    assert r.status_code == 200
    assert client.get("/api/config", headers=_headers(token)).get_json()["data"]["initial_sl_pct"] == 12.5

    r = client.put("/api/config", json={"not_a_setting": 1}, headers=_headers(token))
    assert r.status_code == 400


def test_health_llm_block_when_unconfigured(env, monkeypatch):
    """With LLM env vars unset, /api/health must still return a valid llm block."""
    client, token, cfg = env
    monkeypatch.setattr("app.api.health_api.get_broker", lambda config=None: FakeBroker())
    from app.config import Config as _Cfg
    monkeypatch.setattr(_Cfg, "LLM_API_KEY", "", raising=False)
    monkeypatch.setattr(_Cfg, "LLM_BASE_URL", "", raising=False)
    monkeypatch.setattr(_Cfg, "LLM_MODEL", "", raising=False)
    # Reset persistent counters so the assertion is deterministic.
    from app.strategy.llm_breakout import health as llm_health
    llm_health.reset()

    r = client.get("/api/health", headers=_headers(token))
    assert r.status_code == 200
    llm = r.get_json()["data"]["llm"]
    assert llm["configured"] is False
    assert llm["status"] == "unconfigured"
    assert llm["model"] == ""
    assert llm["stats"]["calls_total"] == 0


def test_health_llm_block_after_success(env, monkeypatch):
    """After a recorded success, /api/health surfaces status=ok with the model slug."""
    client, token, cfg = env
    monkeypatch.setattr("app.api.health_api.get_broker", lambda config=None: FakeBroker())
    from app.config import Config as _Cfg
    monkeypatch.setattr(_Cfg, "LLM_API_KEY", "k", raising=False)
    monkeypatch.setattr(_Cfg, "LLM_BASE_URL", "https://openrouter.ai/api/v1", raising=False)
    monkeypatch.setattr(_Cfg, "LLM_MODEL", "minimax/minimax-m3", raising=False)
    from app.strategy.llm_breakout import health as llm_health
    llm_health.reset()
    llm_health.record_success()
    llm_health.record_success()

    r = client.get("/api/health", headers=_headers(token))
    assert r.status_code == 200
    llm = r.get_json()["data"]["llm"]
    assert llm["configured"] is True
    assert llm["status"] == "ok"
    assert llm["model"] == "minimax/minimax-m3"
    assert llm["stats"]["calls_total"] == 2
    assert llm["stats"]["errors_total"] == 0


def test_health_llm_block_after_error(env, monkeypatch):
    """Last event was an error -> status=error."""
    client, token, cfg = env
    monkeypatch.setattr("app.api.health_api.get_broker", lambda config=None: FakeBroker())
    from app.config import Config as _Cfg
    monkeypatch.setattr(_Cfg, "LLM_API_KEY", "k", raising=False)
    monkeypatch.setattr(_Cfg, "LLM_BASE_URL", "https://openrouter.ai/api/v1", raising=False)
    monkeypatch.setattr(_Cfg, "LLM_MODEL", "minimax/minimax-m3", raising=False)
    from app.strategy.llm_breakout import health as llm_health
    llm_health.reset()
    llm_health.record_success()
    llm_health.record_error("HTTP 500")

    r = client.get("/api/health", headers=_headers(token))
    assert r.status_code == 200
    llm = r.get_json()["data"]["llm"]
    assert llm["status"] == "error"
    assert llm["stats"]["errors_total"] == 1
    assert llm["stats"]["last_error"] == "HTTP 500"