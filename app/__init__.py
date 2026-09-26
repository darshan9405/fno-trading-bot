import logging
import traceback
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

    @app.errorhandler(500)
    def internal_error(e):
        # Catch uncaught exceptions BEFORE Flask renders its default HTML
        # page. The default 500 is a `<!doctype html><html>...Internal Server
        # Error</html>` page which gives the UI no way to surface the real
        # failure. We log the full traceback server-side and return a
        # structured JSON envelope so the UI can show actionable detail.
        from flask import current_app
        current_app.logger.exception("unhandled 500: %s", e)
        tb = traceback.format_exc()
        # Cap the traceback string so a 10MB stack doesn't blow the response.
        return (
            jsonify({
                "status": "error",
                "error": {
                    "code": "internal_error",
                    "message": f"{type(e).__name__}: {e}" if e else "Internal server error",
                    "exception_class": type(e).__name__ if e else None,
                    "traceback": tb[-4000:],
                },
            }),
            500,
        )

    @app.errorhandler(Exception)
    def unhandled_exception(e):
        # Flask only invokes registered handlers for HTTPException subclasses
        # and the codes above. A bare `Exception` (e.g. a SQLAlchemy error
        # raised outside an explicit try/except) would otherwise fall through
        # to the default HTML 500 page — wrap that case too.
        # Skip HTTPException subclasses (404, 405, etc.) — those have
        # their own meaning and aren't really "internal errors".
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return e
        return internal_error(e)

    from app.api import register_blueprints

    register_blueprints(app)

    # Lightweight schema-drift check: the project has no Alembic
    # migrations, so model additions land without a migration step.
    # If a column the model expects is missing from the actual table
    # (e.g. `leads.meta`, added when the LLM-tool-call surface was
    # extended), every endpoint that SELECTs it blows up with
    # `no such column`. This hook adds the missing column once at
    # startup so old deployments self-heal on next boot.
    _run_schema_drift_fixes(app)

    @app.get("/api/health/live")
    def health_live():
        return jsonify({"status": "ok", "service": "fno-trading-bot"})

    return app


def _run_schema_drift_fixes(app: Flask) -> None:
    """Add columns the ORM expects but the live DB is missing.

    The repo has no migration framework; instead we diff the live SQLite
    schema against the SQLAlchemy metadata for the small set of columns
    that have been added since the original schema and `ALTER TABLE` them
    in if they're missing. This is intentionally narrow — only nullable
    columns are added, no defaults, no data backfill — so a partial
    failure can't corrupt existing rows.

    Add a new entry below whenever a model gains a column without a
    corresponding migration step.
    """
    from sqlalchemy import inspect, text
    from app.db import init_db

    # Make sure the engine is constructed (lazy in `db.py`).
    init_db()
    from app.db import _engine  # noqa: WPS433 — module-private by design

    if _engine is None:
        return
    inspector = inspect(_engine)
    table_names = set(inspector.get_table_names())

    # Map of (table, column_name) -> (DDL type, nullable)
    expected: dict[tuple[str, str], tuple[str, bool]] = {
        ("leads", "meta"): ("JSON", True),  # Lead.lead_meta mapped_column("meta", JSON)
    }

    fixes_applied: list[str] = []
    for (table, column), (col_type, nullable) in expected.items():
        if table not in table_names:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        if column in existing:
            continue
        nullable_sql = "" if nullable else " NOT NULL"
        sql = f'ALTER TABLE {table} ADD COLUMN "{column}" {col_type}{nullable_sql}'
        try:
            with _engine.begin() as conn:
                conn.execute(text(sql))
            fixes_applied.append(f"{table}.{column} ({col_type}{nullable_sql})")
            app.logger.info("schema-drift: added %s.%s", table, column)
        except Exception as e:  # noqa: BLE001
            app.logger.warning("schema-drift: failed to add %s.%s: %s", table, column, e)

    if fixes_applied:
        app.logger.info("schema-drift: applied fixes: %s", ", ".join(fixes_applied))