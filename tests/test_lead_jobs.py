"""Lead-generation job registry tests: single-active-job flag, manual submit
coordination with the scheduler-driven run.
"""

import threading
from datetime import datetime, timezone
from threading import Thread

import pytest

from app.config import Config
from app import create_app
from app.db import dispose
from app.scheduler import lead_jobs


@pytest.fixture
def env(tmp_path):
    cfg = Config()
    cfg.RATE_LIMIT_ENABLED = False
    cfg.DATABASE_URL = f"sqlite:///{tmp_path / 'lead_jobs.db'}"
    cfg.SECRET_KEY = "test-secret"
    cfg.JWT_MASTER_SECRET = "test-master"
    dispose()
    create_app(cfg)
    yield
    # Always release the lock between tests in case a test leaked.
    lead_jobs.release_generator_lock()
    dispose()


def _reset_state():
    """Force-clear the module-level state so each test starts clean."""
    lead_jobs.release_generator_lock()


def test_acquire_release_round_trip(env):
    _reset_state()
    assert lead_jobs.is_generator_active() is False
    assert lead_jobs.acquire_generator_lock() is True
    assert lead_jobs.is_generator_active() is True
    lead_jobs.release_generator_lock()
    assert lead_jobs.is_generator_active() is False


def test_second_acquire_returns_false_while_active(env):
    _reset_state()
    assert lead_jobs.acquire_generator_lock() is True
    try:
        assert lead_jobs.acquire_generator_lock() is False
        assert lead_jobs.is_generator_active() is True
    finally:
        lead_jobs.release_generator_lock()
    assert lead_jobs.is_generator_active() is False


def test_release_is_idempotent(env):
    _reset_state()
    lead_jobs.acquire_generator_lock()
    lead_jobs.release_generator_lock()
    # Releasing when not held is safe — flag stays False, no exception.
    lead_jobs.release_generator_lock()
    assert lead_jobs.is_generator_active() is False


def test_submit_manual_job_rejects_while_scheduler_run_active(env):
    """If the scheduler-driven `run_lead_generator` has acquired the flag, a
    manual submit must return `started=False` with `code='lead_generation_busy'`
    so the HTTP endpoint can answer 409. The synthetic JobState must report
    status='running' so the UI can attach to it."""

    _reset_state()
    assert lead_jobs.acquire_generator_lock() is True

    job, started = lead_jobs.submit_manual_job()
    assert started is False
    assert job.status == "running"
    assert job.id == "scheduler"
    assert job.error is None

    lead_jobs.release_generator_lock()


def test_submit_manual_job_rejects_second_manual_while_first_active(env):
    """Two manual back-to-back submits must serialise: the second observes the
    first's running job and returns ``started=False``. Once the first completes
    (simulated by calling ``_finish_job`` directly), a third submit is allowed
    to start a fresh run."""

    _reset_state()
    job1, started1 = lead_jobs.submit_manual_job()
    assert started1 is True
    assert job1.status == "running"

    job2, started2 = lead_jobs.submit_manual_job()
    assert started2 is False
    assert job2.id == job1.id, "second submit should report the first job's id"

    # Simulate the in-flight thread finishing: clear active-id + release lock
    # in the same locked critical section the real path uses.
    lead_jobs._finish_job(job1, "done", result={"generated": 0, "checked": 0})
    assert lead_jobs.is_generator_active() is False

    job3, started3 = lead_jobs.submit_manual_job()
    assert started3 is True
    assert job3.id != job1.id
    lead_jobs._finish_job(job3, "done", result={"generated": 0, "checked": 0})


def test_submit_manual_job_does_not_start_thread_when_lock_held(env):
    """If the lock is held (simulating a scheduler tick), `submit_manual_job`
    must NOT spawn a daemon thread that would compete for the lock."""
    _reset_state()
    assert lead_jobs.acquire_generator_lock() is True

    job, started = lead_jobs.submit_manual_job()
    assert started is False
    assert job.status == "running"
    # The job registry has no entry for the synthetic scheduler job.
    assert lead_jobs.get_job("scheduler") is None

    lead_jobs.release_generator_lock()


def test_concurrent_submits_only_one_wins(env):
    """Hammer `submit_manual_job` from many threads at once. Exactly one submit
    should win; every other caller should observe `started=False` with the same
    winning `job_id`."""

    _reset_state()
    winners = []
    losers = []

    def go():
        job, started = lead_jobs.submit_manual_job()
        (winners if started else losers).append((job.id, started))

    threads = [Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1
    assert len(losers) == 7
    winner_id = winners[0][0]
    assert all(lid == winner_id for lid, _ in losers), "all losers must report the winner's id"

    # Cleanup so the test fixture finishes cleanly.
    lead_jobs.release_generator_lock()


def test_mark_done_no_lock_does_not_release_lock_held_by_another_run(env):
    """Regression: the `result is None` branch in `_run_in_thread` previously
    called `_finish_job`, which unconditionally cleared `_generator_active`.
    When the inner `run_lead_generator` returned None because a scheduler tick
    already held the lock, that would silently release the OTHER run's flag
    and let two lead-generator runs execute concurrently.

    The fix splits the bookkeeping into `_finish_job` (which assumes the
    caller holds the lock and releases it) and `_mark_done_no_lock` (which
    only stamps the job terminal state). `_run_in_thread` now uses the latter
    in the None branch.
    """

    _reset_state()

    # Simulate another run that acquired the lock but has not finished yet.
    assert lead_jobs.acquire_generator_lock() is True
    assert lead_jobs.is_generator_active() is True

    sentinel = lead_jobs.JobState(
        id="orphan",
        status="running",
        submitted_at=lead_jobs._now(),
    )
    # `_run_in_thread`'s None branch uses this helper. It must NOT release the
    # lock — the other run still owns it.
    lead_jobs._mark_done_no_lock(sentinel, "error", error="lead_generation_busy")

    assert lead_jobs.is_generator_active() is True, (
        "_mark_done_no_lock released a lock it never acquired; this would let "
        "two runs execute concurrently"
    )
    assert sentinel.status == "error"

    lead_jobs.release_generator_lock()
    assert lead_jobs.is_generator_active() is False


def test_run_in_thread_does_not_double_acquire_the_lock(env, monkeypatch):
    """Regression: `_run_in_thread` previously called `acquire_generator_lock`
    and then `run_lead_generator`, which itself acquires the lock. The inner
    acquire returned False and the manual run silently produced zero leads.
    After the fix, `_run_in_thread` must let `run_lead_generator` own the
    lock lifecycle end-to-end."""

    _reset_state()
    calls = []

    def fake_run_lead_generator(broker=None, now=None, force=False,
                                on_progress=None, on_tool_call=None,
                                job_id=None, clear_session=False):
        calls.append((broker, now, force, job_id, clear_session))
        # Mimic the scheduler-held case: `_run_in_thread` sees None and the
        # call budget wasn't wasted calling begin_run on a strategy.
        return None

    monkeypatch.setattr(
        "app.scheduler.lead_generator.run_lead_generator",
        fake_run_lead_generator,
    )

    job = lead_jobs.JobState(
        id="manual-1",
        status="running",
        submitted_at=lead_jobs._now(),
    )
    lead_jobs._run_in_thread(job)
    assert calls == [(None, None, True, "manual-1", True)], \
        "expected exactly one inner call with job_id + clear_session"
    assert job.status == "error"
    assert job.error == "lead_generation_busy"
    # The lock must NOT be held afterwards — the inner call never acquired.
    assert lead_jobs.is_generator_active() is False


def test_run_in_thread_reports_generated_lead_count(env, monkeypatch):
    """Happy-path companion to the regression above: when `run_lead_generator`
    returns a populated result, `_run_in_thread` must surface created/checked
    counts on the JobState — proving the manual path actually produces leads."""

    _reset_state()

    def fake_run_lead_generator(broker=None, now=None, force=False,
                                on_progress=None, on_tool_call=None,
                                job_id=None, clear_session=False):
        # Newer contract: result carries scanned/total/cancelled/cleared
        # alongside created/checked. The JobState only surfaces the
        # sub-dict the UI uses to render the "Generated X leads from Y
        # underlyings" toast.
        return {"created": 3, "checked": 5, "scanned": 7, "total": 7,
                "cancelled": False, "cleared": {"leads": 0, "scans": 0}}

    monkeypatch.setattr(
        "app.scheduler.lead_generator.run_lead_generator",
        fake_run_lead_generator,
    )

    job = lead_jobs.JobState(
        id="manual-2",
        status="running",
        submitted_at=lead_jobs._now(),
    )
    lead_jobs._run_in_thread(job)
    assert job.status == "done"
    assert job.result == {"generated": 3, "checked": 5,
                          "scanned": 7, "total": 7}
    assert lead_jobs.is_generator_active() is False


def test_job_state_to_dict_includes_progress_and_timestamps(env):
    """The UI polls JobState.to_dict() to render the live progress panel.
    Ensure the new `progress` and `started_at` fields round-trip through the
    serialiser — a regression here would silently hide progress from the UI.
    """
    from datetime import datetime, timezone

    submitted = datetime(2026, 9, 22, 5, 0, 0, tzinfo=timezone.utc)
    started = datetime(2026, 9, 22, 5, 0, 1, tzinfo=timezone.utc)
    job = lead_jobs.JobState(
        id="abc",
        status="running",
        submitted_at=submitted,
        started_at=started,
        progress={"phase": "analyzing", "scanned": 3, "total": 8,
                  "created": 1, "checked": 1, "errors": 0,
                  "current": "NIFTY", "strategy": "llm_breakout",
                  "recent": []},
    )
    d = job.to_dict()
    assert d["submitted_at"] == submitted.isoformat()
    assert d["started_at"] == started.isoformat()
    assert d["finished_at"] is None
    assert d["progress"]["phase"] == "analyzing"
    assert d["progress"]["total"] == 8
    assert d["progress"]["recent"] == []


def test_run_in_thread_pipes_progress_to_job_state(env, monkeypatch):
    """The `_run_in_thread` callback must copy each progress patch into
    `job.progress` under the registry lock so the UI sees live updates
    even though it polls from a different thread."""

    _reset_state()

    def fake_run_lead_generator(broker=None, now=None, force=False,
                                on_progress=None, on_tool_call=None,
                                job_id=None, clear_session=False):
        assert on_progress is not None, "manual path must pass a callback"
        # Simulate the three callbacks the real generator emits. The
        # newer contract adds `scan_outcomes` (a per-instrument ring
        # buffer) — it lands on the job so the UI's "Scanned stocks"
        # panel can render it without re-querying the REST endpoint.
        on_progress({"phase": "starting", "scanned": 0, "total": 4,
                     "created": 0, "checked": 0, "errors": 0,
                     "current": None, "recent": [], "strategy": "test",
                     "scan_outcomes": []})
        on_progress({"phase": "analyzing", "scanned": 1, "total": 4,
                     "created": 1, "checked": 1, "errors": 0,
                     "current": "FOO", "strategy": "test",
                     "recent": [{"symbol": "FOO", "status": "leads",
                                 "leads": 1, "error": None}],
                     "scan_outcomes": [{"symbol": "FOO", "decision": "generated"}]})
        return {"created": 1, "checked": 1, "scanned": 1, "total": 1,
                "cancelled": False, "cleared": {"leads": 0, "scans": 0}}

    monkeypatch.setattr(
        "app.scheduler.lead_generator.run_lead_generator",
        fake_run_lead_generator,
    )

    job = lead_jobs.JobState(
        id="manual-3",
        status="running",
        submitted_at=lead_jobs._now(),
    )
    lead_jobs._run_in_thread(job)

    # Final state: the second patch must have landed on the job's `progress`
    # field. The "starting" patch is treated as a lifecycle boundary that
    # resets `scan_outcomes` (per `_on_progress`), but the "analyzing" patch
    # merges, so we observe the most recent values.
    assert job.progress["phase"] == "analyzing"
    assert job.progress["scanned"] == 1
    assert job.progress["created"] == 1
    assert job.progress["recent"][0]["symbol"] == "FOO"
    assert job.progress["scan_outcomes"][0]["symbol"] == "FOO"
    assert job.started_at is not None
    assert job.status == "done"


def test_active_job_id_returns_none_when_nothing_in_flight(env):
    """The new manual-attach endpoint relies on `active_job_id()` returning
    None when no manual job is running (even if the generator lock happens
    to be held by a scheduler-driven run).
    """
    _reset_state()
    assert lead_jobs.active_job_id() is None

    # Schedule-driven run holds the lock — still no manual job to attach to.
    assert lead_jobs.acquire_generator_lock() is True
    try:
        assert lead_jobs.active_job_id() is None
    finally:
        lead_jobs.release_generator_lock()


def test_active_job_id_returns_running_manual_job(env, monkeypatch):
    """While a manual job is in flight, `active_job_id()` must return its id
    so the GET-active endpoint can hand it to the UI."""

    _reset_state()
    # Hold the daemon thread inside the fake run until the test has
    # finished asserting on the registry. Without this, the stub returns
    # instantly and the spawned thread races ahead to mark the job done
    # before our `active_job_id()` check.
    blocker = threading.Event()

    def fake_run_lead_generator(*a, **kw):
        blocker.wait(timeout=2.0)
        return {"created": 0, "checked": 0}

    monkeypatch.setattr(
        "app.scheduler.lead_generator.run_lead_generator",
        fake_run_lead_generator,
    )

    job, started = lead_jobs.submit_manual_job()
    assert started is True

    assert lead_jobs.active_job_id() == job.id

    # Finish it via the helper that mimics the thread's terminal path.
    lead_jobs._finish_job(job, "done", result={"generated": 0, "checked": 0})
    assert lead_jobs.active_job_id() is None

    blocker.set()
