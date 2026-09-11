import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, jsonify
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from app.auth import UpstoxTokenStore, configure as configure_auth
from app.config import Config
from app.db import Base, _run_sqlite_migrations, init_db
from app.extensions import limiter
from app.services import market_calendar
from app.settings import seed_default_settings

import app.models  # noqa: F401  (register ORM tables)
import app.strategy  # noqa: F401  (register built-in strategies)


class _ISTFormatter(logging.Formatter):
    """Logging formatter that prints timestamps in IST."""

    def formatTime(self, record, datefmt=None):  # type: ignore[override]
        ts = datetime.fromtimestamp(record.created, tz=ZoneInfo("Asia/Kolkata"))
        if datefmt:
            return ts.strftime(datefmt)
        return ts.strftime("%Y-%m-%d %H:%M:%S %Z")


def create_app(config: Config | None = None) -> Flask:
    config = config or Config()
    app = Flask(__name__)
    app.config.from_object(config)

    # Trust X-Forwarded-* (nginx behind Cloudflare Tunnel): 1 proxy hop.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for handler in logging.root.handlers:
        handler.setFormatter(_ISTFormatter("%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

    engine = init_db(config.DATABASE_URL)
    Base.metadata.create_all(engine)
    _run_sqlite_migrations(engine)
    seed_default_settings()
    market_calendar.seed_defaults()

    configure_auth(config)
    UpstoxTokenStore._ensure_loaded()

    CORS(app, supports_credentials=True, origins=[config.FRONTEND_URL])

    # Rate limiting on all APIs (per client IP; configured via RATELIMIT_* keys).
    app.config["RATELIMIT_ENABLED"] = config.RATE_LIMIT_ENABLED
    app.config["RATELIMIT_DEFAULT"] = config.RATE_LIMIT_DEFAULT
    app.config["RATELIMIT_STORAGE_URI"] = config.RATE_LIMIT_STORAGE_URI
    limiter.init_app(app)

    @app.errorhandler(429)
    def rate_limited(_):
        return (
            jsonify({"status": "error", "error": {"code": "rate_limited", "message": "Rate limit exceeded."}}),
            429,
        )

    from app.api import register_blueprints

    register_blueprints(app)

    @app.get("/api/health/live")
    def health_live():
        return jsonify({"status": "ok", "service": "fno-trading-bot"})

    return app