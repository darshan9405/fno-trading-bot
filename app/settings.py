"""Runtime settings stored in the `settings` table (JSON-encoded values)."""

import json
from typing import Any

from app.config import Config
from app.db import session_scope
from app.models import Setting

DEFAULT_SETTINGS: dict[str, Any] = {
    "strategy": "breakout",
    "trading_start": "10:00",
    "sqoff_time": "14:00",
    "initial_sl_pct": 10.0,
    "trail_activate_pct": 5.0,
    "trail_gap_pct": 5.0,
    "max_lead_price_divergence_pct": 0.5,
    "min_days_to_expiry": 5,
    "strike_selection": "ATM",
    "qty_lots_per_trade": 1,
    "margin_check_enabled": True,
    "margin_max_depth": 3,
    "market_protection_pct": 2,
    "scheduler.lead_generator_seconds": 300,
    "scheduler.trade_tracker_seconds": 30,
    "scheduler.order_placer_seconds": 30,
    "breakout.patterns_enabled": ["volume_breakout"],
    "breakout.min_confidence": 0.7,
    "breakout.lookback_days": 60,
    "breakout.swing_k": 3,
    "breakout.proximity_pct": 0.5,
    "breakout.min_touches": 1,
    "breakout.min_trendline_points": 4,
    "breakout.pole_pct": 3.0,
    # Volume confirmation (Durgia 2025): spike >= multiplier x rolling avg volume.
    "breakout.volume_multiplier": 4.0,
    "breakout.volume_window": 20,
    "breakout.volume_lookback": 5,
    "breakout.require_volume_spike": True,
    "breakout.volume_boost": 0.15,
    # Market-alignment filter: "off" or "nifty_sma20" (trade with the NIFTY trend).
    "breakout.market_alignment": "off",
}


ENV_OVERRIDE_SETTINGS = {
    "trading_start": Config.TRADING_START,
    "sqoff_time": Config.SQOFF_TIME,
}


def _encode(value: Any) -> str:
    return json.dumps(value) if not isinstance(value, str) else value


def _decode(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def seed_default_settings() -> None:
    with session_scope() as session:
        for key, value in DEFAULT_SETTINGS.items():
            if session.get(Setting, key) is None:
                session.add(Setting(key=key, value=_encode(value)))
        for key, value in ENV_OVERRIDE_SETTINGS.items():
            if session.get(Setting, key) is None:
                session.add(Setting(key=key, value=_encode(value)))


def get_setting(key: str, default: Any = None) -> Any:
    with session_scope() as session:
        row = session.get(Setting, key)
        return _decode(row.value) if row else default


def set_setting(key: str, value: Any) -> None:
    with session_scope() as session:
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=_encode(value)))
        else:
            row.value = _encode(value)