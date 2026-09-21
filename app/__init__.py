import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, jsonify
from flask.json.provider import DefaultJSONProvider
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


class _ISOJSONProvider(DefaultJSONProvider):
    """JSON provider that emits ISO 8601 for every date/time/td object.

    Flask's default provider formats ``datetime`` as RFC 1123
    (e.g. ``"Sat, 19 Sep 2026 14:37:00 GMT"``), which the UI's
    ``datetime.fromisoformat`` helper rejects and renders as ``—``.
    Emitting ISO 8601 keeps every timestamp in the API contract
    ``fromisoformat``-compatible across the board.
    """

    def default(self, o):  # type: ignore[override]
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, date):
            return o.isoformat()
        if isinstance(o, time):
            return o.isoformat()
        if isinstance(o, timedelta):
            return o.total_seconds()
        return super().default(o)


def configure_logging(config: Config) -> None:
    """Configure console + persistent file logging for the app."""
    level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
    log_dir = config.LOG_FILE.rsplit("/", 1)[0] if "/" in config.LOG_FILE else "."
    log_path = config.LOG_FILE
    try:
        from logging.handlers import RotatingFileHandler
        import os
        os.makedirs(log_dir, exist_ok=True)
        handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        handler.setLevel(level)
        handler.setFormatter(_ISTFormatter("%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(handler)
    except Exception as e:
        logging.getLogger(__name__).warning("failed to configure file logging: %s", e)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for handler in logging.root.handlers:
        handler.setFormatter(_ISTFormatter("%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))


def create_app(config: Config | None = None) -> Flask:
    config = config or Config()
    app = Flask(__name__)
    app.config.from_object(config)

    # Emit ISO 8601 for datetime/date/time so the UI's ``fromisoformat`` helper
    # can render every timestamp in IST instead of falling back to ``—``.
    app.json = _ISOJSONProvider(app)

    # Trust X-Forwarded-* (nginx behind Cloudflare Tunnel): 1 proxy hop.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    configure_logging(config)

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

    @app.errorhandler(403)
    def forbidden(_):
        return (
            jsonify({"status": "error", "error": {"code": "forbidden", "message": "Forbidden."}}),
            403,
        )

    from app.api import register_blueprints

    register_blueprints(app)

    @app.get("/api/health/live")
    def health_live():
        return jsonify({"status": "ok", "service": "fno-trading-bot"})

    return app