"""Trade-vs-broker drift detection and audit logging.

Every reconciler / trade-tracker tick should call into this module so we
keep an append-only log of *what* drifted, *what we expected*, and *what
the broker said*. The UI surfaces the last 24h of drift events per
trade so operators can post-mortem a bad fill.

Design goals:
  - Pure append-only (never deletes, never overwrites)
  - Cheap: a single SQL insert per event
  - Bounded: cleanup_service trims rows older than DRIFT_RETENTION_DAYS

Drift types we track:
  - "sl_missing"        — our sl_order_id disappeared from broker order book
  - "sl_rejected"       — broker marked our SL as rejected
  - "sl_cancelled"      — broker marked our SL as cancelled (often user)
  - "sl_price_mismatch" — broker SL trigger differs from current_sl
  - "position_missing"  — broker no longer holds the position
  - "qty_mismatch"      — broker quantity differs from trade.quantity
  - "exit_price_mismatch" — broker fill price vs what we recorded
  - "external_sell"     — broker shows a SELL on our token we didn't place
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.db import session_scope
from app.models import Trade, TradeDrift

log = logging.getLogger(__name__)

DRIFT_RETENTION_DAYS = 30


def _truncate(s: str, n: int = 500) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[: n - 3] + "..."


def record_drift(
    *,
    drift_type: str,
    detail: str,
    trade_id: int | None = None,
    instrument_token: str | None = None,
    severity: str = "info",
    expected: Any = None,
    actual: Any = None,
    source: str = "reconciler",
) -> TradeDrift | None:
    """Append a drift event. Returns the row, or None on DB failure."""
    try:
        with session_scope() as session:
            row = TradeDrift(
                trade_id=trade_id,
                instrument_token=instrument_token,
                drift_type=str(drift_type)[:40],
                severity=str(severity)[:16],
                detail=_truncate(detail),
                expected=_truncate(json.dumps(expected, default=str)) if expected is not None else None,
                actual=_truncate(json.dumps(actual, default=str)) if actual is not None else None,
                source=str(source)[:32],
            )
            session.add(row)
            session.flush()
            return row
    except Exception as e:  # noqa: BLE001
        log.warning("drift_service: failed to record drift %s: %s", drift_type, e)
        return None


def list_drifts(
    *,
    trade_id: int | None = None,
    drift_type: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
) -> list[TradeDrift]:
    """Read drift events for UI surfacing. Defaults to last 24h, newest first."""
    since = since or (datetime.utcnow() - timedelta(hours=24))
    with session_scope() as session:
        q = select(TradeDrift).where(TradeDrift.ts >= since)
        if trade_id is not None:
            q = q.where(TradeDrift.trade_id == trade_id)
        if drift_type:
            q = q.where(TradeDrift.drift_type == drift_type)
        rows = session.execute(
            q.order_by(TradeDrift.ts.desc()).limit(max(1, min(limit, 1000)))
        ).scalars().all()
        # Detach so caller can access attrs after the session closes.
        for r in rows:
            session.expunge(r)
        return rows


def cleanup_old_drifts() -> int:
    """Trim drift rows older than DRIFT_RETENTION_DAYS. Returns rows deleted."""
    from sqlalchemy import delete
    cutoff = datetime.utcnow() - timedelta(days=DRIFT_RETENTION_DAYS)
    try:
        with session_scope() as session:
            res = session.execute(
                delete(TradeDrift).where(TradeDrift.ts < cutoff)
            )
            return int(res.rowcount or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("drift_service: cleanup_old_drifts failed: %s", e)
        return 0


def reconcile_against_broker(broker, trade: Trade) -> list[str]:
    """Compare a single trade's DB view to the broker and record any drift.

    Returns the list of drift types recorded so the caller can act on them.
    Safe to call from trade_tracker / reconciler / UI — never raises.
    """
    drift_types: list[str] = []
    if trade.status != "open":
        return drift_types

    # 1. SL order sanity.
    if trade.sl_order_id:
        try:
            book = broker.get_order_book()
        except Exception as e:
            log.warning("drift: get_order_book failed for trade %s: %s", trade.id, e)
            book = []
        sl_view = next((o for o in book if o.order_id == trade.sl_order_id), None)
        if sl_view is None:
            drift_types.append("sl_missing")
            record_drift(
                drift_type="sl_missing",
                trade_id=trade.id,
                instrument_token=trade.option_instrument_key,
                severity="critical",
                detail=(f"SL order {trade.sl_order_id} not found in broker order book; "
                        f"position is now unprotected until next reconcile."),
                expected={"sl_order_id": trade.sl_order_id, "current_sl": trade.current_sl},
                actual="<missing>",
                source="trade_tracker",
            )
        else:
            if sl_view.status in ("rejected", "cancelled"):
                drift_types.append(f"sl_{sl_view.status}")
                record_drift(
                    drift_type=f"sl_{sl_view.status}",
                    trade_id=trade.id,
                    instrument_token=trade.option_instrument_key,
                    severity="critical",
                    detail=(f"SL order {trade.sl_order_id} status={sl_view.status}; "
                            f"message={sl_view.status_message or '-'}"),
                    expected={"status": "open"},
                    actual={"status": sl_view.status, "message": sl_view.status_message},
                    source="trade_tracker",
                )
            elif (sl_view.trigger_price is not None
                  and abs(float(sl_view.trigger_price) - float(trade.current_sl or 0)) > 0.05):
                drift_types.append("sl_price_mismatch")
                record_drift(
                    drift_type="sl_price_mismatch",
                    trade_id=trade.id,
                    instrument_token=trade.option_instrument_key,
                    severity="warn",
                    detail=(f"Broker SL trigger {sl_view.trigger_price} differs from "
                            f"our current_sl {trade.current_sl}"),
                    expected={"current_sl": float(trade.current_sl or 0)},
                    actual={"trigger_price": float(sl_view.trigger_price)},
                    source="trade_tracker",
                )

    # 2. Position sanity.
    try:
        positions = broker.get_positions()
    except Exception as e:
        log.warning("drift: get_positions failed for trade %s: %s", trade.id, e)
        positions = []
    pos = next(
        (p for p in positions if getattr(p, "instrument_token", None) == trade.option_instrument_token),
        None,
    )
    if pos is None:
        drift_types.append("position_missing")
        record_drift(
            drift_type="position_missing",
            trade_id=trade.id,
            instrument_token=trade.option_instrument_key,
            severity="critical",
            detail=(f"Broker reports no position for {trade.tradingsymbol} "
                    f"but DB has trade {trade.id} open."),
            expected={"qty": trade.quantity},
            actual={"qty": 0},
            source="trade_tracker",
        )
    else:
        broker_qty = int(getattr(pos, "quantity", 0) or 0)
        if broker_qty != int(trade.quantity or 0):
            drift_types.append("qty_mismatch")
            record_drift(
                drift_type="qty_mismatch",
                trade_id=trade.id,
                instrument_token=trade.option_instrument_key,
                severity="warn",
                detail=(f"Quantity drift: broker={broker_qty} db={trade.quantity} "
                        f"({trade.tradingsymbol})"),
                expected={"qty": int(trade.quantity or 0)},
                actual={"qty": broker_qty},
                source="trade_tracker",
            )

    return drift_types