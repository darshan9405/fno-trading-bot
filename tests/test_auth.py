"""Auth tests: JWT daily rotation, SSO callback -> cookies, bootstrap, refresh."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import jwt
import pytest

from app import auth as auth_module
from app.config import Config
from app.db import dispose
from app import create_app


@pytest.fixture
def auth_env(tmp_path):
    db_file = tmp_path / "auth.db"
    cfg = Config()
    cfg.RATE_LIMIT_ENABLED = False
    cfg.DATABASE_URL = f"sqlite:///{db_file}"
    cfg.SECRET_KEY = "test-secret-key"
    cfg.JWT_MASTER_SECRET = "test-master-secret"
    cfg.JWT_ACCESS_TTL_MINUTES = 15
    cfg.JWT_REFRESH_TTL_DAYS = 7
    cfg.FRONTEND_URL = "http://localhost:8501"
    cfg.COOKIE_SECURE = False
    cfg.UPSTOX_CLIENT_ID = "client-1"
    cfg.UPSTOX_CLIENT_SECRET = "client-secret"
    cfg.UPSTOX_REDIRECT_URI = "http://localhost:8000/api/auth/upstox/callback"
    cfg.UPSTOX_API_VERSION = "2.0"

    auth_module.configure(cfg)
    from app.auth import UpstoxTokenStore

    UpstoxTokenStore._token = None
    UpstoxTokenStore._loaded = False

    dispose()
    app = create_app(cfg)
    app.config["TESTING"] = True

    from flask import jsonify, request

    @app.get("/api/_test/protected")
    @auth_module.jwt_required
    def _protected():
        return jsonify({"status": "ok", "user_id": request.auth_user_id})

    with app.test_client() as client:
        yield client, cfg
    dispose()


def _jwt_cfg():
    cfg = Config()
    cfg.RATE_LIMIT_ENABLED = False
    cfg.JWT_MASTER_SECRET = "unit-secret"
    cfg.JWT_ACCESS_TTL_MINUTES = 15
    return cfg


# --- JWT unit tests ------------------------------------------------------


def test_issue_and_verify_access_jwt():
    auth_module.configure(_jwt_cfg())
    token = auth_module.issue_access_jwt("usr-1")
    payload = auth_module.verify_access_jwt(token)
    assert payload is not None
    assert payload["sub"] == "usr-1"


def test_daily_key_rotation_rejects_yesterday_token():
    auth_module.configure(_jwt_cfg())
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    y_token = auth_module.issue_access_jwt("usr-1", day=yesterday)

    assert auth_module.verify_access_jwt(y_token) is None  # rejected today

    # but it is still valid under yesterday's daily key -> proves key rotation
    payload = jwt.decode(y_token, auth_module.daily_key(yesterday), algorithms=["HS256"])
    assert payload["day"] == yesterday


def test_expired_jwt_rejected():
    auth_module.configure(_jwt_cfg())
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "usr-1",
        "day": now.date().isoformat(),
        "iat": now - timedelta(minutes=5),
        "exp": now - timedelta(minutes=1),
    }
    token = jwt.encode(payload, auth_module.daily_key(), algorithm="HS256")
    assert auth_module.verify_access_jwt(token) is None


def test_upstox_token_expiry_from_jwt():
    import base64
    import json

    from app.auth import upstox_token_expiry

    def b64(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode()

    # exp 1791151200 = 2026-10-05 03:30 IST = 22:00 UTC on 2026-10-04
    token = f"h.{b64({'exp': 1791151200})}.s"
    assert upstox_token_expiry(token) == datetime(2026, 10, 4, 22, 0)
    # malformed / no exp -> None
    assert upstox_token_expiry("garbage") is None
    assert upstox_token_expiry(f"h.{b64({'sub': 'u'})}.s") is None


def test_upstox_token_store_expiry(auth_env):
    import base64
    import json

    from app.auth import UpstoxTokenStore

    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1791151200}).encode()).decode()
    token = f"h.{payload}.s"
    UpstoxTokenStore.set(token)
    assert UpstoxTokenStore.get() == token
    assert UpstoxTokenStore.get_expiry() == datetime(2026, 10, 4, 22, 0)


# --- API flow tests ------------------------------------------------------


def test_login_redirects_to_upstox(auth_env):
    client, cfg = auth_env
    resp = client.get("/api/auth/upstox/login")
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location.startswith("https://api.upstox.com/v2/login/authorization/dialog")
    assert "client_id=client-1" in location


def test_status_unauthenticated(auth_env):
    client, cfg = auth_env
    resp = client.get("/api/auth/status")
    assert resp.get_json()["data"]["authenticated"] is False


def test_full_ss_flow(auth_env, monkeypatch):
    client, cfg = auth_env

    class FakeLoginApi:
        def __init__(self, api_client=None):
            pass

        def token(self, api_version, **kwargs):
            return SimpleNamespace(access_token="upstox-access", user_id="usr-1", user_name="Trader")

    monkeypatch.setattr("upstox_client.LoginApi", FakeLoginApi)

    # callback -> 302 to frontend, cookies set, bootstrap code in query
    resp = client.get("/api/auth/upstox/callback?code=thecode")
    assert resp.status_code == 302
    cookies = resp.headers.getlist("Set-Cookie")
    assert any(c.startswith("upstox_at=") for c in cookies)
    assert any(c.startswith("upstox_rt=") for c in cookies)
    assert resp.headers["Location"].startswith("http://localhost:8501")

    bootstrap = parse_qs(urlparse(resp.headers["Location"]).query)["bootstrap"][0]

    # status now authenticated via cookie
    assert client.get("/api/auth/status").get_json()["data"]["authenticated"] is True

    # bootstrap exchange (one-time) -> tokens for UI session_state
    resp = client.post("/api/auth/bootstrap", json={"code": bootstrap})
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    access, refresh = data["access_token"], data["refresh_token"]
    assert data["user_id"] == "usr-1"

    # bootstrap code is single-use
    assert client.post("/api/auth/bootstrap", json={"code": bootstrap}).status_code == 401

    # protected route via Authorization header
    r = client.get("/api/_test/protected", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200
    assert r.get_json()["user_id"] == "usr-1"

    # protected route via cookie (fresh client shares jar? use cookie on same client)
    r = client.get("/api/_test/protected")
    assert r.status_code == 200

    # unauthorized without token
    assert client.get("/api/_test/protected", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_refresh_rotates_token(auth_env, monkeypatch):
    client, cfg = auth_env

    class FakeLoginApi:
        def __init__(self, api_client=None):
            pass

        def token(self, api_version, **kwargs):
            return SimpleNamespace(access_token="upstox-access", user_id="usr-1", user_name="Trader")

    monkeypatch.setattr("upstox_client.LoginApi", FakeLoginApi)
    loc = client.get("/api/auth/upstox/callback?code=code2").headers["Location"]
    boot = parse_qs(urlparse(loc).query)["bootstrap"][0]
    refresh = client.post("/api/auth/bootstrap", json={"code": boot}).get_json()["data"]["refresh_token"]

    # refresh rotates
    resp = client.post("/api/auth/refresh", headers={"X-Refresh-Token": refresh})
    assert resp.status_code == 200
    new_refresh = resp.get_json()["data"]["refresh_token"]
    assert new_refresh != refresh
    assert resp.get_json()["data"]["access_token"]

    # old refresh is now invalid
    assert client.post("/api/auth/refresh", headers={"X-Refresh-Token": refresh}).status_code == 401
    # new one works
    assert client.post("/api/auth/refresh", headers={"X-Refresh-Token": new_refresh}).status_code == 200


def test_logout_revokes_refresh(auth_env, monkeypatch):
    client, cfg = auth_env

    class FakeLoginApi:
        def __init__(self, api_client=None):
            pass

        def token(self, api_version, **kwargs):
            return SimpleNamespace(access_token="upstox-access", user_id="usr-1", user_name="Trader")

    monkeypatch.setattr("upstox_client.LoginApi", FakeLoginApi)
    loc = client.get("/api/auth/upstox/callback?code=code2").headers["Location"]
    boot = parse_qs(urlparse(loc).query)["bootstrap"][0]
    refresh = client.post("/api/auth/bootstrap", json={"code": boot}).get_json()["data"]["refresh_token"]

    assert client.post("/api/auth/logout", headers={"X-Refresh-Token": refresh}).status_code == 200
    assert client.post("/api/auth/refresh", headers={"X-Refresh-Token": refresh}).status_code == 401