"""Rate limiting tests: Flask-Limiter returns 429 + our error envelope."""

import pytest
from flask import jsonify

from app import create_app
from app.config import Config
from app.db import dispose
from app.extensions import limiter


@pytest.fixture
def env(tmp_path):
    cfg = Config()
    cfg.DATABASE_URL = f"sqlite:///{tmp_path / 'rl.db'}"
    cfg.SECRET_KEY = "test-secret"
    cfg.JWT_MASTER_SECRET = "test-master"
    cfg.RATE_LIMIT_ENABLED = True
    cfg.RATE_LIMIT_DEFAULT = "60 per minute"
    dispose()
    app = create_app(cfg)
    app.config["TESTING"] = True

    @app.get("/api/_test/limited")
    @limiter.limit("2 per minute")
    def _limited():
        return jsonify({"status": "ok"})

    with app.test_client() as client:
        yield client
    dispose()


def test_rate_limit_returns_429(env):
    client = env
    assert client.get("/api/_test/limited").status_code == 200
    assert client.get("/api/_test/limited").status_code == 200
    r = client.get("/api/_test/limited")  # 3rd within the minute
    assert r.status_code == 429
    body = r.get_json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "rate_limited"