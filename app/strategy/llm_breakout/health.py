"""LLM call stats persistence + health-snapshot helper.

Tracks cumulative counters and the most recent success/error timestamps so the
health API can show whether the LLM is connected and last-called the provider
recently. Stored as a single JSON blob in the `settings` table under the
reserved key `llm.health`.

Defensive: every public function tolerates DB errors (returns a safe default)
so a misbehaving health check never breaks the scheduler.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

SETTING_KEY = "llm.health"


def _empty_stats() -> dict[str, Any]:
    return {
        "calls_total": 0,
        "errors_total": 0,
        "last_success_at": None,
        "last_error_at": None,
        "last_error": None,
    }


def get_stats() -> dict[str, Any]:
    """Read the current LLM call stats. Returns the zero-state when unset or
    on any DB failure."""
    try:
        from app.settings import get_setting
        raw = get_setting(SETTING_KEY, None)
    except Exception as exc:  # noqa: BLE001
        log.warning("llm_health: read failed: %s", exc)
        return _empty_stats()
    if not isinstance(raw, dict):
        return _empty_stats()
    merged = _empty_stats()
    merged.update({k: raw.get(k, merged[k]) for k in merged})
    return merged


def _write_stats(stats: dict[str, Any]) -> None:
    try:
        from app.settings import set_setting
        set_setting(SETTING_KEY, stats)
    except Exception as exc:  # noqa: BLE001
        log.warning("llm_health: write failed: %s", exc)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def record_success() -> None:
    """Increment the success counter and stamp `last_success_at`."""
    stats = get_stats()
    stats["calls_total"] = int(stats.get("calls_total", 0)) + 1
    stats["last_success_at"] = _utcnow_iso()
    _write_stats(stats)


def record_error(error: str) -> None:
    """Increment the error counter and stamp `last_error_at` + `last_error`."""
    stats = get_stats()
    stats["errors_total"] = int(stats.get("errors_total", 0)) + 1
    stats["last_error_at"] = _utcnow_iso()
    stats["last_error"] = (error or "")[:500]
    _write_stats(stats)


def reset() -> None:
    """Wipe all LLM stats (mostly useful for tests / ops)."""
    _write_stats(_empty_stats())


def get_health_snapshot() -> dict[str, Any]:
    """Build the JSON the health API serves under `data["llm"]`.

    Combines the persistent counters with live config / connectivity info so
    the UI can render a single tile per dimension:
      - configured  : True iff env-driven Config has all of API_KEY/BASE_URL/MODEL
      - model       : the configured model slug (or "")
      - base_url    : the configured endpoint (or "")
      - stats       : counters + last-success/last-error timestamps
      - status      : one of "unconfigured" | "ok" | "error" | "stale" | "unknown"
    """
    from app.config import Config

    cfg = Config()
    configured = bool(cfg.LLM_API_KEY and cfg.LLM_BASE_URL and cfg.LLM_MODEL)
    stats = get_stats()

    status = "unconfigured"
    last_success_at = stats.get("last_success_at")
    last_error_at = stats.get("last_error_at")
    if configured:
        if last_success_at and not last_error_at:
            status = "ok"
        elif last_success_at and last_error_at:
            status = "error"  # most-recent event was an error
        elif last_error_at and not last_success_at:
            status = "error"
        else:
            status = "unknown"  # configured but never called

    return {
        "configured": configured,
        "model": cfg.LLM_MODEL,
        "base_url": cfg.LLM_BASE_URL,
        "stats": stats,
        "status": status,
    }
