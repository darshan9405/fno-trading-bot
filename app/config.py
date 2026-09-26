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

    # Single-user allowlist: set to your Upstox user_id (find it at
    # GET /api/health -> broker.user_id). Empty => allow all (dev/test).
    ALLOWED_UPSTOX_USER_ID = os.getenv("ALLOWED_UPSTOX_USER_ID", "")

    TRADING_START = os.getenv("TRADING_START", "10:00")
    SQOFF_TIME = os.getenv("SQOFF_TIME", "14:00")
    # Hard cutoff after which the order placer stops opening NEW trades.
    # Existing positions are still managed by trade_tracker (squared off at
    # SQOFF_TIME). Must be <= SQOFF_TIME.
    TRADE_END_TIME = os.getenv("TRADE_END_TIME", "11:00")

    # LLM breakout detector. Required env when `llm.enabled = True`; the
    # strategy short-circuits and emits no leads if any of API_KEY / BASE_URL
    # / MODEL is missing. BASE_URL should be the OpenAI-compatible root
    # (no trailing slash, no path); the client appends `/chat/completions`.
    #
    # We talk to the MiniMax M3 provider DIRECTLY (not via OpenRouter) so we
    # keep full control over tool-calling, retries, and cost. MiniMax exposes
    # an OpenAI-compatible chat-completions endpoint; we POST to
    # `${LLM_BASE_URL}/chat/completions` with Bearer auth.
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.minimax.io/v1")
    LLM_MODEL = os.getenv("LLM_MODEL", "MiniMax-M3")
    LLM_TIMEOUT_S = _as_float(os.getenv("LLM_TIMEOUT_S"), 60.0)
    LLM_MAX_RETRIES = _as_int(os.getenv("LLM_MAX_RETRIES"), 2)
    # Reasoning / "thinking" controls. Empty / 0 disables the field so models
    # that don't understand it don't reject the request.
    LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "medium")
    LLM_REASONING_MAX_TOKENS = _as_int(os.getenv("LLM_REASONING_MAX_TOKENS"), 2000)
    # Cap the agent-loop iteration depth so a runaway tool-call sequence
    # cannot burn the whole context budget. The detector short-circuits as
    # soon as the model returns a final answer.
    LLM_AGENT_MAX_ITERATIONS = _as_int(os.getenv("LLM_AGENT_MAX_ITERATIONS"), 8)
    # Bing news search for the `fetch_news` tool. If `LLM_BING_API_KEY` is
    # set we hit the Bing News Search API; otherwise we scrape the public
    # Bing News search HTML (no key required).
    LLM_BING_API_KEY = os.getenv("LLM_BING_API_KEY", "")
    # Optional LLM provider app-attribution headers (some providers use them
    # for analytics and rate-limit bumps). Off by default.
    LLM_APP_URL = os.getenv("LLM_APP_URL", "")
    LLM_APP_NAME = os.getenv("LLM_APP_NAME", "FnO Trading Bot")

    # API rate limiting (Flask-Limiter).
    RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
    RATE_LIMIT_DEFAULT = os.getenv("RATE_LIMIT_DEFAULT", "60 per minute")
    RATE_LIMIT_STORAGE_URI = os.getenv("RATE_LIMIT_STORAGE_URI", "memory://")

    # Auto-seed the instruments whitelist from the Upstox instrument master.
    # S1 uses this as an empty-table safety net; the Docker entrypoint is the
    # primary seeder. Set "1" in compose/.env for production.
    AUTO_SEED_INSTRUMENTS = os.getenv("AUTO_SEED_INSTRUMENTS", "0").lower() == "1"

    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE = os.getenv("LOG_FILE", "logs/trading.log")

    # Square-off order type. Upstox rejects plain MARKET SELL orders via the
    # API (UDAPI1028). We always exit via a Stop-Loss-Limit (SL) order with
    # trigger at LTP and limit priced below by `SQOFF_SL_LIMIT_OFFSET_PCT`.
    # If LTP is unavailable we fall back to a plain LIMIT @ current_sl.
    SQOFF_SL_LIMIT_OFFSET_PCT = _as_float(os.getenv("SQOFF_SL_LIMIT_OFFSET_PCT"), 1.0)
    # Square-off retry policy — how many times we retry a rejected SL
    # square-off before falling back to LIMIT.
    SQOFF_MAX_RETRIES = _as_int(os.getenv("SQOFF_MAX_RETRIES"), 2)
    # Minimum interval between two reconciler passes (seconds). The actual
    # scheduler interval is also tied to this — we cap it here to avoid
    # over-polling the broker.
    RECONCILER_MIN_INTERVAL_S = _as_int(os.getenv("RECONCILER_MIN_INTERVAL_S"), 15)
    # Reconciliation pass must wait this long after the entry fill before it
    # trusts the broker positions endpoint (Upstox propagation lag).
    RECON_MIN_AGE_MINUTES = _as_int(os.getenv("RECON_MIN_AGE_MINUTES"), 2)

    @property
    def is_dev(self):
        return self.FLASK_ENV != "production"