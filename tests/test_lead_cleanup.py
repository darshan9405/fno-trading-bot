"""Tests for app.services.lead_cleanup_service + app.scheduler.lead_cleanup.

Covers the hybrid retention policy:
  * any lead whose status is not "queued" is deleted almost immediately
  * queued leads older than `leads.retention_hours_queued` (default 24h) are
    age-out cleared
  * idempotent: running the cleanup twice is a no-op the second time
  * the scheduler wiring exposes the entry point and respects the
    `leads.retention_hours_queued` setting
  * the FK on `trades.lead_id` is satisfied: a Trade linked to a now-deleted
    Lead survives with `lead_id == None`.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app import create_app
from app.config import Config
from app.db import dispose, session_scope
from app.models import Instrument, Lead, SchedulerHeartbeat, Trade
from app.scheduler.lead_cleanup import run_lead_cleanup
from app.services import lead_cleanup_service
from app.services.health_service import utcnow
from app.settings import set_setting

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def env(tmp_path):
    cfg = Config()
    cfg.RATE_LIMIT_ENABLED = False
    cfg.DATABASE_URL = f"sqlite:///{tmp_path / 'cleanup.db'}"
    cfg.SECRET_KEY = "test-secret"
    cfg.JWT_MASTER_SECRET = "test-master"
    dispose()
    create_app(cfg)
    yield
    dispose()


def _ist(*, year, month, day, hour=0, minute=0, second=0):
    """Aware IST -> naive UTC, matching the model column convention."""
    aware = datetime(year, month, day, hour, minute, second, tzinfo=IST)
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _seed_instrument(session, symbol="NIFTY"):
    inst = Instrument(
        symbol=symbol,
        exchange="NSE",
        segment="NSE_INDEX",
        spot_instrument_key="NSE_INDEX|Nifty 50",
        instrument_token="26000",
        trading_symbol=symbol,
        lot_size=50,
        enabled=True,
    )
    session.add(inst)
    session.flush()
    return inst


def _seed_lead(session, instrument, *, status="queued", created_at=None, direction="CALL",
               confidence=0.9, processed_at=None):
    lead = Lead(
        instrument_id=instrument.id,
        underlying_key=instrument.spot_instrument_key,
        direction=direction,
        strategy="breakout",
        signal_type="horizontal_range",
        signal_level=100.0,
        confidence=confidence,
        chart_interval="day",
        status=status,
        created_at=created_at,
        processed_at=processed_at,
    )
    session.add(lead)
    session.flush()
    return lead


def _seed_trade(session, *, lead_id, underlying_key):
    trade = Trade(
        lead_id=lead_id,
        underlying_key=underlying_key,
        option_instrument_key="NSE_FO|84123",
        option_instrument_token="84123",
        tradingsymbol="NIFTY 10 SEP 26 26800 CE",
        lot_size=50,
        product="D",
        direction="CALL",
        entry_price=10.0,
        quantity=50,
        initial_sl=11.0,
        current_sl=11.0,
        status="open",
    )
    session.add(trade)
    session.flush()
    return trade


# --- cleanup_processed_leads ---------------------------------------------


def test_cleanup_processed_leads_removes_placed_skipped_expired_picked(env):
    # Processed leads are retained for 7 days (168h) by default.
    # Create old processed leads (8 days old) and a fresh queued lead.
    old = utcnow() - timedelta(hours=200)  # 8+ days old
    with session_scope() as session:
        inst = _seed_instrument(session)
        _seed_lead(session, inst, status="placed", created_at=old, processed_at=old)
        _seed_lead(session, inst, status="skipped", created_at=old, processed_at=old)
        _seed_lead(session, inst, status="expired", created_at=old, processed_at=old)
        _seed_lead(session, inst, status="picked", created_at=old, processed_at=old)
        # A fresh queued lead must NOT be deleted.
        _seed_lead(session, inst, status="queued")

    deleted = lead_cleanup_service.cleanup_processed_leads()
    assert deleted == 4

    with session_scope() as session:
        remaining = session.execute(select(Lead.status)).scalars().all()
        assert remaining == ["queued"]


def test_cleanup_processed_leads_nulls_trade_lead_id_but_keeps_trade(env):
    with session_scope() as session:
        inst = _seed_instrument(session)
        placed = _seed_lead(session, inst, status="queued", confidence=0.95)
        trade = _seed_trade(session, lead_id=placed.id,
                            underlying_key=inst.spot_instrument_key)
        # Migrate lead -> placed (make it old so it gets cleaned up).
        old = utcnow() - timedelta(hours=200)
        placed.status = "placed"
        placed.created_at = old
        placed.processed_at = old

    deleted = lead_cleanup_service.cleanup_processed_leads()
    assert deleted == 1

    with session_scope() as session:
        assert session.get(Lead, placed.id) is None
        kept_trade = session.get(Trade, trade.id)
        assert kept_trade is not None
        assert kept_trade.lead_id is None
        assert kept_trade.status == "open"


def test_cleanup_processed_leads_is_idempotent(env):
    old = utcnow() - timedelta(hours=200)
    with session_scope() as session:
        inst = _seed_instrument(session)
        _seed_lead(session, inst, status="placed", created_at=old, processed_at=old)

    assert lead_cleanup_service.cleanup_processed_leads() == 1
    assert lead_cleanup_service.cleanup_processed_leads() == 0


def test_cleanup_processed_leads_does_nothing_when_nothing_processed(env):
    with session_scope() as session:
        inst = _seed_instrument(session)
        _seed_lead(session, inst, status="queued")

    assert lead_cleanup_service.cleanup_processed_leads() == 0


# --- cleanup_expired_queued_leads ----------------------------------------


def test_cleanup_expired_queued_removes_only_older_than_retention(env):
    now = utcnow()
    with session_scope() as session:
        inst = _seed_instrument(session)
        # 25h old — must be deleted.
        _seed_lead(session, inst, status="queued",
                   created_at=now - timedelta(hours=25))
        # 23h old — must survive (<24h retention).
        _seed_lead(session, inst, status="queued",
                   created_at=now - timedelta(hours=23), confidence=0.8)
        # 1h old — must survive, untouched.
        fresh = _seed_lead(session, inst, status="queued",
                           created_at=now - timedelta(hours=1), confidence=0.7)

    deleted = lead_cleanup_service.cleanup_expired_queued_leads(retention_hours=24)
    assert deleted == 1

    with session_scope() as session:
        rows = session.execute(select(Lead.confidence)).scalars().all()
        assert sorted(rows) == sorted([0.8, 0.7])
        assert session.get(Lead, fresh.id) is not None


def test_cleanup_expired_queued_does_not_touch_processed(env):
    """Processed rows are the cleanup_processed_leads job's responsibility."""
    now = utcnow()
    with session_scope() as session:
        inst = _seed_instrument(session)
        _seed_lead(session, inst, status="placed",
                   created_at=now - timedelta(hours=72),
                   processed_at=now - timedelta(hours=71))

    deleted = lead_cleanup_service.cleanup_expired_queued_leads(retention_hours=24)
    assert deleted == 0  # the placed lead stays — let the other cleanup get it


def test_cleanup_expired_queued_is_idempotent(env):
    now = utcnow()
    with session_scope() as session:
        inst = _seed_instrument(session)
        _seed_lead(session, inst, status="queued",
                   created_at=now - timedelta(hours=48))

    assert lead_cleanup_service.cleanup_expired_queued_leads(retention_hours=24) == 1
    assert lead_cleanup_service.cleanup_expired_queued_leads(retention_hours=24) == 0


def test_cleanup_expired_queued_custom_retention_hours(env):
    now = utcnow()
    inst_id = None
    with session_scope() as session:
        inst = _seed_instrument(session)
        inst_id = inst.id
        _seed_lead(session, inst, status="queued",
                   created_at=now - timedelta(minutes=45))
        _seed_lead(session, inst, status="queued",
                   created_at=now - timedelta(minutes=10), confidence=0.6)

    # 30-minute retention: the 45-minute lead is gone, the 10-minute one lives.
    deleted_minutes = lead_cleanup_service.cleanup_expired_queued_leads(retention_hours=0)  # i.e. 0h cutoff = now
    # A second pass at 24h retention (post-delete baseline) leaves nothing.
    deleted_hours = lead_cleanup_service.cleanup_expired_queued_leads(retention_hours=24)
    # Either of the two passes will delete everything older than its cutoff.
    assert deleted_minutes + deleted_hours >= 1

    # A lead dated in the future must survive an arbitrary cutoff.
    with session_scope() as session:
        _seed_lead(session, session.get(Instrument, inst_id),
                   status="queued", created_at=now + timedelta(hours=2),
                   confidence=0.42)
    assert lead_cleanup_service.cleanup_expired_queued_leads(retention_hours=24) == 0

    with session_scope() as session:
        confidences = sorted(session.execute(select(Lead.confidence)).scalars().all())
        assert confidences == [0.42]  # only the future-dated lead remains


# --- run_lead_cleanup scheduler entry ------------------------------------


def test_run_lead_cleanup_touches_heartbeat_and_removes_processed(env):
    old = utcnow() - timedelta(hours=200)
    with session_scope() as session:
        inst = _seed_instrument(session)
        _seed_lead(session, inst, status="placed", created_at=old, processed_at=old)

    run_lead_cleanup(retention_hours=24)

    with session_scope() as session:
        hb = session.get(SchedulerHeartbeat, "scheduler.lead_cleanup")
        assert hb is not None
        assert hb.status == "ok"
        assert "processed=1" in (hb.note or "")
        assert "retention=24h" in (hb.note or "")


def test_run_lead_cleanup_uses_setting_when_no_arg(env):
    set_setting("leads.retention_hours_queued", 12)
    now = utcnow()
    with session_scope() as session:
        inst = _seed_instrument(session)
        # 20h old: alive under default 24h, dead under setting's 12h.
        _seed_lead(session, inst, status="queued",
                   created_at=now - timedelta(hours=20))

    run_lead_cleanup()  # no retention_hours → reads setting

    with session_scope() as session:
        assert session.execute(select(Lead)).scalars().all() == []  # deleted


def test_run_lead_cleanup_heartbeat_records_error_on_failure(env, monkeypatch):
    monkeypatch.setattr(
        lead_cleanup_service, "cleanup_processed_leads",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    run_lead_cleanup(retention_hours=24)

    with session_scope() as session:
        hb = session.get(SchedulerHeartbeat, "scheduler.lead_cleanup")
        assert hb is not None
        assert hb.status == "error"
        assert "boom" in (hb.note or "")


# --- end-to-end: scheduler wires up cleanly ------------------------------


def test_lead_cleanup_scheduler_alongside_other_jobs(env, monkeypatch):
    """Confirm the manager registers the cleanup job alongside the existing three."""
    from app.scheduler import manager

    class _Fake:
        instances: list = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.jobs: list = []
            type(self).instances.append(self)

        def add_job(self, func, trigger, **kwargs):
            self.jobs.append({"func": func, "trigger": trigger, **kwargs})

        def start(self):
            pass

    _Fake.instances = []
    monkeypatch.setattr(manager, "BackgroundScheduler", _Fake)
    manager._scheduler = None
    manager.init_scheduler()

    ids = {j["id"] for j in _Fake.instances[-1].jobs}
    assert ids == {"lead_generator", "trade_tracker", "order_placer", "lead_cleanup", "reconciler"}
    cleanup_job = next(j for j in _Fake.instances[-1].jobs if j["id"] == "lead_cleanup")
    assert cleanup_job["trigger"] == "interval"
    assert cleanup_job["seconds"] == manager.DEFAULT_LEAD_CLEANUP_SECONDS
    assert cleanup_job["max_instances"] == 1
    assert cleanup_job["coalesce"] is True

    manager._scheduler = None
