"""Runtime settings stored in the `settings` table (JSON-encoded values)."""

import json
from typing import Any

from app.config import Config
from app.db import session_scope
from app.models import Setting

DEFAULT_SETTINGS: dict[str, Any] = {
    "strategy": "llm_breakout",
    "trading_start": "10:00",
    # Hard cutoff after which the order placer stops opening NEW trades.
    # Existing positions are still managed by trade_tracker (squared off at
    # sqoff_time). Must be <= sqoff_time.
    "trade_end_time": "11:00",
    "sqoff_time": "14:00",
    "initial_sl_pct": 10.0,
    "trail_activate_pct": 20.0,
    "trail_gap_pct": 10.0,
    "max_lead_price_divergence_pct": 0.5,
    "min_days_to_expiry": 5,
    "strike_selection": "ATM",
    "qty_lots_per_trade": 1,
    "margin_check_enabled": True,
    "margin_max_depth": 3,
    # Entry order (LIMIT) placement & fill polling.
    "entry_order_fill_timeout_seconds": 30,
    "entry_limit_premium_pct": 1.0,
    # Scheduler intervals (seconds). Read at startup by app.scheduler.manager.
    "scheduler.lead_generator_seconds": 300,
    "scheduler.trade_tracker_seconds": 30,
    "scheduler.order_placer_seconds": 30,
    # Lead retention: the cleanup scheduler removes processed leads immediately,
    # and deletes any queued lead older than this many hours.
    "scheduler.lead_cleanup_seconds": 30,
    "leads.retention_hours_queued": 24,
    "leads.retention_hours_processed": 168,
    # Lead generator run bounds (configurable from the UI via /api/config).
    # The generator iterates a shuffled slice of the enabled-instrument list and
    # stops as soon as it has persisted this many leads — keeps the scheduler
    # tick fast and respects limited deployment capital. 0 disables the cap.
    "lead_generator.max_leads_per_run": 5,
    "lead_generator.shuffle_instruments": True,
    # Concurrent worker threads that fetch candles + run the strategy in
    # parallel. Each worker gets its own strategy instance (the LLM detector
    # keeps per-instance call state). UpstoxBroker's per-process rate limiter
    # is thread-safe so concurrent candle requests stay under budget. Capped
    # at 1 minimum to keep the test environment deterministic.
    "lead_generator.max_workers": 4,
    # Reconciliation: read broker truth (get_positions + get_order_book) and close
    # DB trades whose position is gone. Runs every 60s by default. Trades younger
    # than `reconciler_min_age_minutes` are skipped to give Upstox time to
    # propagate fresh entries.
    "scheduler.reconciler_seconds": 60,
    "scheduler.reconciler_min_age_minutes": 1,
    # LLM breakout detector (replaces the math-based detectors entirely).
    # `llm.enabled` is the master switch. When False, no leads are generated.
    # The other knobs are interpolated into the LLM's system prompt at call
    # time, so tuning them in the DB affects the next run.
    "llm.enabled": True,
    "llm.lookback_candles": 250,            # 200-250 daily bars to slice into the prompt
    "llm.min_confidence": 0.7,              # floor for LLM-reported confidence
    "llm.temperature": 0.2,                 # OpenAI-compat sampling temperature
    "llm.max_calls_per_run": 50,            # hard cap so a slow LLM can't block the scheduler tick
    # Tier-4 staleness: half-life (minutes) of queued lead confidence decay; 0 disables.
    "breakout.staleness_half_life_min": 0,
    # Tier-4 calibration: alpha multiplier applied to historical win-rate; 0 disables.
    "scoring.calibration_alpha": 0.0,
    # Tier-3 add-ons:
    #   - time_of_day: ON by default. Heston-Sadka-Sadka (2010) — strongest intraday
    #     continuation effect, cheapest to compute (no broker call). Flows into the
    #     composite score at lead-generation time.
    #   - oi / iv: ON by default. They need the option chain (strike-level IV/OI),
    #     which is already loaded in `attach_lead_plans` once the strike is resolved
    #     — so they're computed contract-specifically (not on a guessed strike) and
    #     added to the lead's stored score without an extra broker call.
    "scoring.enable_iv": True,
    "scoring.enable_oi": True,
    "scoring.enable_time_of_day": True,
}


ENV_OVERRIDE_SETTINGS = {
    "trading_start": Config.TRADING_START,
    "trade_end_time": Config.TRADE_END_TIME,
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
    # One-time migration: previous schema used the legacy "breakeven + best_price trail"
    # defaults (trail_activate_pct=5.0, trail_gap_pct=5.0). Operators who never touched
    # those rows get the new "activate past SL + ltp trail" defaults automatically.
    # Operators who set non-default values are NOT bumped.
    _LEGACY_TRAIL_ACTIVATE_PCT = 5.0
    _LEGACY_TRAIL_GAP_PCT = 5.0
    # The math-based "breakout" package was removed when the LLM detector landed;
    # bump existing deployments so the scheduler stops crashing with
    # `Unknown strategy: 'breakout'` on a persistent DB.
    _LEGACY_STRATEGY = "breakout"

    with session_scope() as session:
        for key, value in DEFAULT_SETTINGS.items():
            if session.get(Setting, key) is None:
                session.add(Setting(key=key, value=_encode(value)))
        for key, value in ENV_OVERRIDE_SETTINGS.items():
            if session.get(Setting, key) is None:
                session.add(Setting(key=key, value=_encode(value)))

        # Bump legacy defaults so existing deployments get the new behaviour.
        legacy_bump = {
            "trail_activate_pct": DEFAULT_SETTINGS["trail_activate_pct"],
            "trail_gap_pct": DEFAULT_SETTINGS["trail_gap_pct"],
            "strategy": DEFAULT_SETTINGS["strategy"],
        }
        legacy_values = {
            "trail_activate_pct": _LEGACY_TRAIL_ACTIVATE_PCT,
            "trail_gap_pct": _LEGACY_TRAIL_GAP_PCT,
            "strategy": _LEGACY_STRATEGY,
        }
        for key, new_value in legacy_bump.items():
            row = session.get(Setting, key)
            if row is None:
                continue
            current = _decode(row.value)
            if current == legacy_values[key]:
                row.value = _encode(new_value)


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