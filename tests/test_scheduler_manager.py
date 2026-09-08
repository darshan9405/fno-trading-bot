"""Tests for the scheduler manager: intervals come from `settings`."""

import pytest

from app import create_app
from app.config import Config
from app.db import dispose, session_scope
from app.scheduler import manager as scheduler
from app.settings import set_setting


@pytest.fixture
def env(tmp_path):
    cfg = Config()
    cfg.RATE_LIMIT_ENABLED = False
    cfg.DATABASE_URL = f"sqlite:///{tmp_path / 'mgr.db'}"
    cfg.SECRET_KEY = "test-secret"
    cfg.JWT_MASTER_SECRET = "test-master"
    dispose()
    create_app(cfg)
    yield
    dispose()


class _FakeScheduler:
    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.jobs: list = []
        self.started = False
        type(self).instances.append(self)

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})
        return None

    def start(self):
        self.started = True


@pytest.fixture
def _patched_scheduler(monkeypatch, env):
    _FakeScheduler.instances = []
    monkeypatch.setattr(scheduler, "BackgroundScheduler", _FakeScheduler)
    scheduler._scheduler = None
    yield _FakeScheduler
    scheduler._scheduler = None


def test_manager_uses_default_intervals_when_settings_absent(_patched_scheduler):
    scheduler.init_scheduler()
    assert len(_FakeScheduler.instances) == 1
    jobs = {j["id"]: j for j in _FakeScheduler.instances[0].jobs}
    assert jobs["lead_generator"]["trigger"] == "interval"
    assert jobs["lead_generator"]["seconds"] == scheduler.DEFAULT_LEAD_GENERATOR_SECONDS
    assert jobs["trade_tracker"]["seconds"] == scheduler.DEFAULT_TRADE_TRACKER_SECONDS
    assert jobs["order_placer"]["seconds"] == scheduler.DEFAULT_ORDER_PLACER_SECONDS


def test_manager_reads_intervals_from_settings(_patched_scheduler):
    set_setting("scheduler.lead_generator_seconds", 120)
    set_setting("scheduler.trade_tracker_seconds", 15)
    set_setting("scheduler.order_placer_seconds", 45)

    scheduler.init_scheduler()
    jobs = {j["id"]: j for j in _FakeScheduler.instances[-1].jobs}
    assert jobs["lead_generator"]["seconds"] == 120
    assert jobs["trade_tracker"]["seconds"] == 15
    assert jobs["order_placer"]["seconds"] == 45


def test_manager_is_idempotent(_patched_scheduler):
    scheduler.init_scheduler()
    first = scheduler._scheduler
    scheduler.init_scheduler()
    assert scheduler._scheduler is first
    assert len(_FakeScheduler.instances) == 1
