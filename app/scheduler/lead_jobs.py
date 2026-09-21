"""In-memory registry of manual lead-generation jobs.

The UI button (POST /api/trades/leads/generate) used to run `run_lead_generator`
inline, which blocked the request thread and frequently tripped the client-side
10s timeout when several underlyings were enabled. Now the endpoint dispatches
the work to a background daemon thread and returns immediately with a
`job_id`; the UI polls GET /api/trades/leads/generate/<job_id> until the job
finishes (or errors).

Concurrency policy: at most one manual run at a time. A second submission
during an in-flight run returns the same `job_id` with `status=running` (HTTP
409 from the endpoint), so spam-clicks can't pile up jobs — matches the
`max_instances=1` discipline already used by the APScheduler-managed
`lead_generator` job in `scheduler/manager.py`.

Storage: a single module-level `dict` keyed by `job_id`. We retain only the
last few jobs (`MAX_RETAINED`) so the dict doesn't grow without bound.
"""

import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal

log = logging.getLogger(__name__)

JobStatus = Literal["running", "done", "error"]

# How many recent jobs we keep in memory. Older jobs are forgotten, so the
# /leads/generate/<id> endpoint will return 404 for them. 5 is plenty for a
# manual on-demand button.
MAX_RETAINED = 5

_lock = threading.Lock()
_jobs: dict[str, "JobState"] = {}
# The most recent job that ended up running (vs. just getting the 409
# short-circuit). Lets `_active_job()` answer "is something in flight?" in O(1).
_active_id: str | None = None
# Process-wide gate: True iff a lead-generation run is in flight — regardless of
# whether it was triggered by APScheduler (interval job) or the manual "Generate
# now" UI endpoint. Both paths acquire/release this flag so they cannot overlap
# even though APScheduler's `max_instances=1` only blocks two scheduler ticks
# (not a manual run that arrives while a scheduler tick is mid-flight).
_generator_active: bool = False


@dataclass
class JobState:
    id: str
    status: JobStatus
    submitted_at: datetime
    finished_at: datetime | None = None
    result: dict | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["submitted_at"] = self.submitted_at.isoformat()
        if self.finished_at is not None:
            d["finished_at"] = self.finished_at.isoformat()
        return d


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _trim() -> None:
    if len(_jobs) <= MAX_RETAINED:
        return
    # Drop oldest finished jobs first; never evict the active one.
    finished = [(j.submitted_at, jid) for jid, j in _jobs.items() if j.status != "running"]
    finished.sort()
    for _, jid in finished[: len(_jobs) - MAX_RETAINED]:
        _jobs.pop(jid, None)


def _set_active(job_id: str | None) -> None:
    global _active_id
    _active_id = job_id


def _active_job() -> "JobState | None":
    if _active_id is None:
        return None
    job = _jobs.get(_active_id)
    if job is None or job.status != "running":
        return None
    return job


def is_generator_active() -> bool:
    """True iff any lead-generation run is in flight (manual or scheduler)."""
    return _generator_active


def acquire_generator_lock() -> bool:
    """Claim the single-job slot. Returns False if another run is in flight.

    Callers MUST pair this with ``release_generator_lock()`` in a try/finally
    so the flag is always released, even when run_lead_generator raises.
    """
    global _generator_active
    with _lock:
        if _generator_active:
            return False
        _generator_active = True
        return True


def release_generator_lock() -> None:
    global _generator_active
    with _lock:
        _generator_active = False


def _finish_job(job: JobState, status: JobStatus, result: dict | None = None, error: str | None = None) -> None:
    """Stamps job terminal state, clears the active-id pointer, AND releases
    the generator lock — all under a single `_lock` acquisition so a racing
    `submit_manual_job` doesn't see an inconsistent view (lock released +
    `_active_id` still set).

    Precondition: the caller MUST hold the generator lock. The
    `result is None` short-circuit path in `_run_in_thread` is the exception;
    it calls `_mark_done_no_lock` instead because the lock belongs to the
    OTHER run that preempted us.
    """
    global _generator_active
    with _lock:
        job.status = status
        if result is not None:
            job.result = result
        if error is not None:
            job.error = error
        job.finished_at = _now()
        _set_active(None)
        _generator_active = False


def _mark_done_no_lock(job: JobState, status: JobStatus, error: str | None = None) -> None:
    """Stamp a job's terminal state WITHOUT touching the generator lock.

    Used by `_run_in_thread` when `run_lead_generator` returned None — meaning
    the inner acquire failed because a different run (e.g. a scheduler tick)
    already holds the lock. Releasing `_generator_active` here would silently
    clear the OTHER run's flag and let two runs execute concurrently."""
    with _lock:
        job.status = status
        if error is not None:
            job.error = error
        job.finished_at = _now()
        _set_active(None)


def _run_in_thread(job: JobState) -> None:
    from app.scheduler.lead_generator import run_lead_generator

    # The generator lock is acquired inside `run_lead_generator` itself — we
    # must NOT re-acquire it here or the inner acquire returns False and the
    # manual run silently exits with no leads. If another run started between
    # `submit_manual_job`'s pre-flight check and our entry into this thread,
    # `run_lead_generator` returns None; surface that as a busy error.
    try:
        result = run_lead_generator(force=True)
    except Exception as e:
        log.exception("manual lead-generator job %s crashed", job.id)
        _finish_job(job, "error", error=str(e) or e.__class__.__name__)
        return

    if result is None:
        # Lock not acquired → another run still holds it → don't touch the
        # generator flag (the other run owns it); just record the error.
        _mark_done_no_lock(job, "error", error="lead_generation_busy")
        return

    if result.get("error"):
        _finish_job(job, "error", error=str(result["error"]))
    else:
        _finish_job(
            job,
            "done",
            result={"generated": result.get("created", 0), "checked": result.get("checked", 0)},
        )


def submit_manual_job() -> tuple[JobState, bool]:
    """Start a manual lead-generation run.

    Returns ``(job, started)`` — ``started=False`` means another run was already
    in flight (either a prior manual submit still running, or an APScheduler
    tick executing ``run_lead_generator`` right now). The caller should respond
    409 in that case so the UI can attach to the in-flight job's status.
    """
    if is_generator_active():
        existing = _active_job() or JobState(
            id="scheduler",
            status="running",
            submitted_at=_now(),
        )
        return existing, False

    with _lock:
        existing = _active_job()
        if existing is not None:
            return existing, False

        job = JobState(
            id=uuid.uuid4().hex[:12],
            status="running",
            submitted_at=_now(),
        )
        _jobs[job.id] = job
        _set_active(job.id)
        _trim()

    thread = threading.Thread(
        target=_run_in_thread,
        args=(job,),
        name=f"lead-gen-manual-{job.id}",
        daemon=True,
    )
    thread.start()
    return job, True


def get_job(job_id: str) -> JobState | None:
    with _lock:
        return _jobs.get(job_id)
