import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Config:
    FLASK_ENV = os.getenv("FLASK_ENV", "development")
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")

    DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(BASE_DIR / 'fno.db').as_posix()}")

    UPSTOX_CLIENT_ID = os.getenv("UPSTOX_CLIENT_ID", "")
    UPSTOX_CLIENT_SECRET = os.getenv("UPSTOX_CLIENT_SECRET", "")
    UPSTOX_REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "")
    UPSTOX_API_VERSION = os.getenv("UPSTOX_API_VERSION", "2.0")
    # Upstox API base URLs. Production defaults; for sandbox set both to
    # https://api-sandbox.upstox.com.
    UPSTOX_API_BASE = os.getenv("UPSTOX_API_BASE", "https://api.upstox.com")
    UPSTOX_ORDER_BASE = os.getenv("UPSTOX_ORDER_BASE", "https://api-hft.upstox.com")

    # Outbound rate limiting against the Upstox API. Per-process gate enforced
    # inside UpstoxBroker; order endpoints are NOT throttled here.
    UPSTOX_THROTTLING_ENABLED = os.getenv("UPSTOX_THROTTLING_ENABLED", "true").lower() == "true"
    UPSTOX_CANDLES_PER_SECOND = _as_float(os.getenv("UPSTOX_CANDLES_PER_SECOND"), 1.0)
    UPSTOX_LTP_PER_SECOND = _as_float(os.getenv("UPSTOX_LTP_PER_SECOND"), 10.0)
    UPSTOX_OPTION_CONTRACTS_PER_SECOND = _as_float(os.getenv("UPSTOX_OPTION_CONTRACTS_PER_SECOND"), 2.0)

    # Post-SSO redirect target for the UI (Streamlit) and CORS origin.
    FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:8501")
    # Internal API base the UI uses for server-side calls (compose: http://backend:8000).
    BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
    # Browser-accessible API base (for the SSO login link the UI renders).
    # Dev: http://localhost:8000  |  Prod (single origin): same as FRONTEND_URL.
    PUBLIC_API_URL = os.getenv("PUBLIC_API_URL", "http://localhost:8000")
    # Mark cookies Secure when serving over HTTPS (Cloudflare Tunnel terminates TLS).
    COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"

    JWT_MASTER_SECRET = os.getenv("JWT_MASTER_SECRET", "dev-master-secret")
    JWT_ACCESS_TTL_MINUTES = _as_int(os.getenv("JWT_ACCESS_TTL_MINUTES"), 15)
    JWT_REFRESH_TTL_DAYS = _as_int(os.getenv("JWT_REFRESH_TTL_DAYS"), 7)

    TRADING_START = os.getenv("TRADING_START", "10:00")
    SQOFF_TIME = os.getenv("SQOFF_TIME", "14:00")

    # API rate limiting (Flask-Limiter).
    RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
    RATE_LIMIT_DEFAULT = os.getenv("RATE_LIMIT_DEFAULT", "60 per minute")
    RATE_LIMIT_STORAGE_URI = os.getenv("RATE_LIMIT_STORAGE_URI", "memory://")

    # Auto-seed the instruments whitelist from the Upstox instrument master.
    # S1 uses this as an empty-table safety net; the Docker entrypoint is the
    # primary seeder. Set "1" in compose/.env for production.
    AUTO_SEED_INSTRUMENTS = os.getenv("AUTO_SEED_INSTRUMENTS", "0").lower() == "1"

    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    @property
    def is_dev(self):
        return self.FLASK_ENV != "production"