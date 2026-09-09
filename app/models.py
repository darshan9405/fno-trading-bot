"""Stage 1: DB schema for the F&O trading system.

All tables mirror the verified Upstox SDK response models (see docs/design.md).
Timestamps are stored in UTC; trading window fields are IST wall-clock strings.
"""

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Broker / config tables -----------------------------------------------


class Instrument(Base):
    """Whitelisted underlying used for pattern detection (spot series only)."""

    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    exchange: Mapped[str] = mapped_column(String(16), default="NSE")
    segment: Mapped[str] = mapped_column(String(16))  # NSE_EQ | NSE_INDEX
    spot_instrument_key: Mapped[str] = mapped_column(String(64), unique=True)
    instrument_token: Mapped[str] = mapped_column(String(32))  # v2 numeric token
    trading_symbol: Mapped[str] = mapped_column(String(64))
    lot_size: Mapped[int] = mapped_column(Integer, default=1)  # F&O lot size
    tick_size: Mapped[float] = mapped_column(Float, default=0.05)
    chart_interval: Mapped[str] = mapped_column(String(16), default="day")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class Setting(Base):
    """Runtime key/value configuration (JSON-encoded values)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")  # JSON-encoded


class MarketHoliday(Base):
    """Non-trading days (NSE holidays, synced from Upstox + fixed civil defaults)."""

    __tablename__ = "market_holidays"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    note: Mapped[str | None] = mapped_column(String(128), nullable=True)


class SpecialSession(Base):
    """Per-day trading-window override (special/half-day sessions)."""

    __tablename__ = "special_sessions"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    start: Mapped[str] = mapped_column(String(8))  # "HH:MM" IST
    end: Mapped[str] = mapped_column(String(8))  # "HH:MM" IST
    note: Mapped[str | None] = mapped_column(String(128), nullable=True)


class SchedulerHeartbeat(Base):
    """Last-run health signal for each APScheduler job."""

    __tablename__ = "scheduler_heartbeats"

    scheduler: Mapped[str] = mapped_column(String(32), primary_key=True)  # lead_generator | trade_tracker | order_placer
    last_run_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    status: Mapped[str] = mapped_column(String(16), default="ok")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


# --- Signals --------------------------------------------------------------


class Lead(Base):
    """A strategy-detected signal awaiting order placement."""

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), index=True)
    underlying_key: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(String(8))  # CALL | PUT
    strategy: Mapped[str] = mapped_column(String(32), default="breakout")
    signal_type: Mapped[str] = mapped_column(String(32))  # strategy-specific label, e.g. horizontal_range | trendline | ...
    signal_level: Mapped[float] = mapped_column(Float)  # the trigger price that fired the signal
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    chart_interval: Mapped[str] = mapped_column(String(16), default="day")
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | picked | placed | skipped | expired
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    instrument: Mapped["Instrument"] = relationship()
    trade: Mapped["Trade | None"] = relationship(back_populates="lead", uselist=False)


# --- Execution ------------------------------------------------------------


class Trade(Base):
    """A single managed delivery (NRML) F&O trade (option leg)."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True, unique=True)
    underlying_key: Mapped[str] = mapped_column(String(64), index=True)
    option_instrument_key: Mapped[str] = mapped_column(String(64), index=True)  # NSE_FO|...
    option_instrument_token: Mapped[str] = mapped_column(String(32), default="")
    tradingsymbol: Mapped[str] = mapped_column(String(64))
    lot_size: Mapped[int] = mapped_column(Integer)
    product: Mapped[str] = mapped_column(String(8), default="D")  # delivery (NRML)
    direction: Mapped[str] = mapped_column(String(8))  # CALL | PUT

    entry_price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[int] = mapped_column(Integer)
    initial_sl: Mapped[float] = mapped_column(Float)
    current_sl: Mapped[float] = mapped_column(Float)
    trail_state: Mapped[str] = mapped_column(String(16), default="at_initial")  # at_initial | breakeven | trailing
    # Extreme favorable price used for trailing (highest for CALL, lowest for PUT).
    best_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sl_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sl_order_type: Mapped[str | None] = mapped_column(String(8), nullable=True)  # SL-M | SL (set at placement)

    status: Mapped[str] = mapped_column(String(16), default="open")  # open | closed | sqoff | killed
    entry_time: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)  # sl_hit | trailing_sl | sqoff | killswitch | manual
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    lead: Mapped["Lead | None"] = relationship(back_populates="trade")
    orders: Mapped[list["Order"]] = relationship(back_populates="trade")


class Order(Base):
    """Order audit mirror of Upstox OrderData."""

    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), index=True)
    order_request_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_type: Mapped[str] = mapped_column(String(16))  # MARKET | LIMIT | SL | SL-M
    variety: Mapped[str] = mapped_column(String(16), default="regular")
    transaction_type: Mapped[str] = mapped_column(String(8))  # BUY | SELL
    product: Mapped[str] = mapped_column(String(8))
    price: Mapped[float] = mapped_column(Float, default=0.0)
    trigger_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    instrument_token: Mapped[str] = mapped_column(String(64))
    tradingsymbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exchange: Mapped[str] = mapped_column(String(16), default="NSE")
    validity: Mapped[str] = mapped_column(String(8), default="DAY")
    is_amo: Mapped[bool] = mapped_column(Boolean, default=False)
    tag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    order_timestamp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    exchange_timestamp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    trade: Mapped["Trade"] = relationship(back_populates="orders")
    fills: Mapped[list["OrderFill"]] = relationship(back_populates="order")


class OrderFill(Base):
    """Fill-level audit mirror of Upstox TradeData."""

    __tablename__ = "order_fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    upstox_trade_id: Mapped[str] = mapped_column(String(64), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"), index=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    instrument_token: Mapped[str] = mapped_column(String(64))
    tradingsymbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transaction_type: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    average_price: Mapped[float] = mapped_column(Float)
    order_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    product: Mapped[str | None] = mapped_column(String(8), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(16), nullable=True)
    exchange_timestamp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    order: Mapped["Order"] = relationship(back_populates="fills")


class OptionContract(Base):
    """Cached option chain row for an underlying (refreshed daily before trading)."""

    __tablename__ = "option_cache"
    __table_args__ = (
        UniqueConstraint("underlying_key", "expiry", "strike_price", "call_or_put", name="uq_option_cache"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    underlying_key: Mapped[str] = mapped_column(String(64), index=True)
    expiry: Mapped[date] = mapped_column(Date)
    strike_price: Mapped[float] = mapped_column(Float)
    instrument_key: Mapped[str] = mapped_column(String(64))  # NSE_FO|...
    lot_size: Mapped[int] = mapped_column(Integer)
    call_or_put: Mapped[str] = mapped_column(String(4))  # CE | PE
    cached_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# --- Account / state ------------------------------------------------------


class FundsSnapshot(Base):
    """Point-in-time funds & margin from Upstox."""

    __tablename__ = "funds_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    available_margin: Mapped[float] = mapped_column(Float)
    used_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    span_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    exposure_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    notional_cash: Mapped[float | None] = mapped_column(Float, nullable=True)


class KillSwitch(Base):
    """Audit trail for killswitch activations."""

    __tablename__ = "killswitches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    triggered_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(32), default="user")  # user | manual | system
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


# --- Auth / ops -----------------------------------------------------------


class AuthToken(Base):
    """Opaque refresh tokens (hashed) used to rotate daily JWT access tokens."""

    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_type: Mapped[str] = mapped_column(String(16), default="refresh")  # refresh | access
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class ErrorLog(Base):
    """Error log surfaced by the system-health API."""

    __tablename__ = "errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    source: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    stack: Mapped[str | None] = mapped_column(Text, nullable=True)