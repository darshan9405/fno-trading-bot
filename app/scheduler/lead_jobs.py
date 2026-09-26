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
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # Live progress snapshot updated by the generator thread via
    # `_run_in_thread`'s callback. Shape (all keys optional, but always set
    # together once the first callback fires):
    #   {
    #     "phase": "starting" | "analyzing" | "finalizing",
    #     "scanned": int, "total": int,
    #     "created": int, "checked": int, "errors": int,
    #     "current": str | None,
    #     "recent": [{"symbol": str, "status": "ok"|"leads"|"empty"|"error",
    #                 "leads": int, "error": str | None}, ...],   # capped at 5
    #     "strategy": str,
    #   }
    progress: dict = field(default_factory=dict)
    result: dict | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["submitted_at"] = self.submitted_at.isoformat()
        if self.started_at is not None:
            d["started_at"] = self.started_at.isoformat()
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

    def _on_progress(patch: dict) -> None:
        # The background thread may interleave with the polling HTTP thread;
        # take `_lock` so `job.progress` updates aren't torn (a `dict.update`
        # is not atomic in CPython). The dict is small, so contention is fine.
        with _lock:
            job.progress.update(patch)

    # Per-tool-call LLM events fire from a worker thread while a different
    # instrument is being analyzed. We keep a small ring of the most recent
    # events (capped) on `job.progress["current_tool_calls"]` so the UI can
    # show "tool call X just happened" without overwhelming the 2s polling
    # payload. When the LLM finishes analyzing a symbol the buffer is reset
    # on the next "analyzing" progress event (which always sets
    # `current_tool_calls: []`).
    _LLM_LOG_CAP = 12

    def _on_tool_call(symbol: str, event: dict) -> None:
        try:
            with _lock:
                buf = job.progress.get("current_tool_calls") or []
                # Tag each entry with the symbol it came from so the UI can
                # attribute the tool call when the underlying flips mid-call.
                entry = {
                    "ts": _now().isoformat(),
                    "symbol": symbol,
                    "iter": event.get("iter"),
                    "name": event.get("name"),
                    "args": event.get("args"),
                    "result_keys": event.get("result_keys") or [],
                }
                buf.append(entry)
                if len(buf) > _LLM_LOG_CAP:
                    del buf[: len(buf) - _LLM_LOG_CAP]
                job.progress["current_tool_calls"] = buf
                # Mirror the live underlying so the UI can show "Analysing
                # RELIANCE · last tool: compute_indicators" without needing
                # a separate "current" patch.
                job.progress["current"] = symbol
        except Exception as e:  # noqa: BLE001
            log.debug("lead_jobs: _on_tool_call failed: %s", e)

    with _lock:
        job.started_at = _now()

    # The generator lock is acquired inside `run_lead_generator` itself — we
    # must NOT re-acquire it here or the inner acquire returns False and the
    # manual run silently exits with no leads. If another run started between
    # `submit_manual_job`'s pre-flight check and our entry into this thread,
    # `run_lead_generator` returns None; surface that as a busy error.
    try:
        result = run_lead_generator(force=True,
                                   on_progress=_on_progress,
                                   on_tool_call=_on_tool_call)
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


def active_job_id() -> str | None:
    """Id of the currently-running manual job, if any.

    Distinct from `_active_job()` (which synthesises a fake "scheduler"
    sentinel when a scheduler-driven run holds the flag): the manual-attach
    endpoint only wants to point the UI at a job_id the GET-status route can
    actually answer for. Returns None when the active run is scheduler-driven
    or there is nothing in flight — both cases the UI treats as "no manual
    job to attach to".
    """
    with _lock:
        aid = _active_id
    if aid is None:
        return None
    with _lock:
        job = _jobs.get(aid)
    if job is None or job.status != "running":
        return None
    return job.id
