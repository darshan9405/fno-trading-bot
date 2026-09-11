"""Calibration hooks for the composite scoring model (Tier-4).

When a trade closes, this service upserts the historical win-rate stats into
the `pattern_stats` table keyed by `(pattern, underlying_key)`. The order
placer uses these stats at rank time to nudge the composite score:

    calibrated = base * (1 + alpha * (win_rate - 0.5))

`alpha` is read from the `scoring.calibration_alpha` setting (default 0 = no
calibration). Calibration only kicks in once there's enough history to be
meaningful: we require `>= MIN_TRADES_DEFAULT` closed trades for the (pattern,
underlying) pair. Below that threshold, calibration is skipped — patterns with
no history are treated as 50% win-rate neutral.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db import session_scope
from app.models import Lead, PatternStat, Trade

MIN_TRADES_DEFAULT = 30


def upsert_pattern_stat(session, *, pattern: str, underlying_key: str) -> PatternStat | None:
    """Recompute `pattern_stats` row for `(pattern, underlying_key)` after a
    trade close. Called from `trade_service.close_trade`.

    `Win` here is defined as `realized_pnl > 0`. We only consider closed trades
    joined back to a `Lead` whose `signal_type` matches `pattern`.
    """
    try:
        rows = session.execute(
            select(Trade).join(Lead, Lead.id == Trade.lead_id).where(
                Lead.signal_type == pattern,
                Trade.underlying_key == underlying_key,
                Trade.status.in_(("closed", "sqoff", "killed")),
            )
        ).scalars().all()
    except Exception:
        return None

    if not rows:
        return None

    wins = sum(1 for t in rows if (t.realized_pnl or 0.0) > 0.0)
    total_pnl = sum(float(t.realized_pnl or 0.0) for t in rows)

    stat = session.execute(
        select(PatternStat).where(
            PatternStat.pattern == pattern,
            PatternStat.underlying_key == underlying_key,
        )
    ).scalars().first()
    if stat is None:
        stat = PatternStat(
            pattern=pattern,
            underlying_key=underlying_key,
            trades=len(rows),
            wins=wins,
            total_pnl=round(total_pnl, 2),
            avg_pnl=round(total_pnl / max(len(rows), 1), 4),
        )
        session.add(stat)
    else:
        stat.trades = len(rows)
        stat.wins = wins
        stat.total_pnl = round(total_pnl, 2)
        stat.avg_pnl = round(total_pnl / max(len(rows), 1), 4)
    return stat


def calibration_multiplier(pattern: str, underlying_key: str, alpha: float,
                           min_trades: int = MIN_TRADES_DEFAULT) -> float:
    """Multiplier in `[1 - 0.5*alpha, 1 + 0.5*alpha]` returned from
    historical win-rate. Returns 1.0 (neutral) when calibration is off or
    insufficient history.

    Computed multiplicatively in the order placer; here we expose the raw
    multiplier so callers can apply whichever shape they like.
    """
    if alpha <= 0:
        return 1.0
    try:
        with session_scope() as session:
            stat = session.execute(
                select(PatternStat).where(
                    PatternStat.pattern == pattern,
                    PatternStat.underlying_key == underlying_key,
                )
            ).scalars().first()
    except Exception:
        return 1.0
    if stat is None or stat.trades < min_trades:
        return 1.0
    rate = max(0.0, min(1.0, stat.wins / max(stat.trades, 1)))
    # Symmetric: 50% wins -> 1.0, 0% -> (1 - 0.5*alpha), 100% -> (1 + 0.5*alpha)
    return max(0.5, min(1.5, 1.0 + alpha * (rate - 0.5)))
