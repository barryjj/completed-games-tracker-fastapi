"""
In-process job tracker for background tasks (currently: Steam library syncs).

This is intentionally lightweight — no Redis, no Celery, no DB rows. Jobs live
in memory for the lifetime of the server process. If the server restarts mid-sync
the job is lost; the next sync will pick up where it left off because the sync
itself is idempotent (existing entries get updated, not duplicated).

Concurrency: protected by a single module-level Lock since FastAPI sync routes
run in a thread pool. Reads/writes are short, so contention is a non-issue.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from enum import StrEnum
from threading import Lock


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclasses.dataclass
class Job:
    id: str
    user_id: int
    kind: str  # e.g. "steam_sync_full", "steam_sync_games"
    label: str = ""  # human-readable label for UI display, e.g. "Steam sync"
    status: JobStatus = JobStatus.QUEUED
    message: str | None = None
    error: str | None = None
    notified: bool = False  # client has seen the completion toast for this job
    progress: dict | None = None  # live progress, updated mid-run: {done, total, title}
    created_at: datetime.datetime = dataclasses.field(default_factory=lambda: datetime.datetime.now(datetime.UTC))
    finished_at: datetime.datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in (JobStatus.DONE, JobStatus.FAILED)


_jobs: dict[str, Job] = {}
_lock = Lock()


def create(user_id: int, kind: str, label: str = "") -> Job:
    job = Job(id=str(uuid.uuid4()), user_id=user_id, kind=kind, label=label)
    with _lock:
        _jobs[job.id] = job
    return job


def get(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def update(job_id: str, **changes) -> Job | None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        for k, v in changes.items():
            setattr(job, k, v)
        return job


def mark_done(job_id: str, message: str) -> Job | None:
    return update(
        job_id,
        status=JobStatus.DONE,
        message=message,
        finished_at=datetime.datetime.now(datetime.UTC),
    )


def mark_failed(job_id: str, error: str) -> Job | None:
    return update(
        job_id,
        status=JobStatus.FAILED,
        error=error,
        finished_at=datetime.datetime.now(datetime.UTC),
    )


def active_jobs_for(user_id: int) -> list[Job]:
    """Jobs currently queued or running for this user."""
    with _lock:
        return [j for j in _jobs.values() if j.user_id == user_id and j.status in (JobStatus.QUEUED, JobStatus.RUNNING)]


def pending_notifications_for(user_id: int) -> list[Job]:
    """Terminal jobs the user hasn't been notified about yet. Marks them notified."""
    with _lock:
        pending = [j for j in _jobs.values() if j.user_id == user_id and j.is_terminal and not j.notified]
        for j in pending:
            j.notified = True
        return pending


def clear_all() -> None:
    """Test helper: wipe the registry."""
    with _lock:
        _jobs.clear()
