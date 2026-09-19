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


def _run_in_thread(job: JobState) -> None:
    from app.scheduler.lead_generator import run_lead_generator

    try:
        result = run_lead_generator(force=True) or {}
    except Exception as e:
        log.exception("manual lead-generator job %s crashed", job.id)
        with _lock:
            job.status = "error"
            job.error = str(e) or e.__class__.__name__
            job.finished_at = _now()
            _set_active(None)
        return

    with _lock:
        job.finished_at = _now()
        if result.get("error"):
            job.status = "error"
            job.error = str(result["error"])
        else:
            job.status = "done"
            job.result = {"generated": result.get("created", 0), "checked": result.get("checked", 0)}
        _set_active(None)


def submit_manual_job() -> tuple[JobState, bool]:
    """Start a manual lead-generation run.

    Returns ``(job, started)`` — ``started=False`` means another run was already
    in flight and ``job`` is that existing job (caller should respond 409).
    """
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
