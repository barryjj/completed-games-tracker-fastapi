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
    error_code: str | None = None  # machine-readable failure class, e.g. "steam_cookies_expired"
    notified: bool = False  # client has seen the completion toast for this job
    progress: dict | None = None  # live progress, updated mid-run: {done, total, title}
    cancel_requested: bool = False
    created_at: datetime.datetime = dataclasses.field(default_factory=lambda: datetime.datetime.now(datetime.UTC))
    finished_at: datetime.datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in (JobStatus.DONE, JobStatus.FAILED)


_jobs: dict[str, Job] = {}
_lock = Lock()


# Uploads waiting on the mapping wizard. The file is read once, held here
# while the user checks what the parser found, and dropped as soon as it is
# either imported or abandoned. Process memory, gone on restart, which is fine
# for something that only has to survive one round trip (#197).
_staged: dict[str, tuple[int, str, bytes]] = {}


def stage_upload(user_id: int, filename: str, file_bytes: bytes) -> str:
    """Hold an uploaded file for the mapping step. Returns its token."""
    token = str(uuid.uuid4())
    with _lock:
        # One at a time per user: a second upload replaces the first, so an
        # abandoned wizard cannot pin a file in memory forever.
        for tok, (uid, _, _) in list(_staged.items()):
            if uid == user_id:
                del _staged[tok]
        _staged[token] = (user_id, filename, file_bytes)
    return token


def staged_upload(token: str, user_id: int) -> tuple[str, bytes] | None:
    """The staged file, if the token is this user's."""
    with _lock:
        item = _staged.get(token)
    if not item or item[0] != user_id:
        return None
    return item[1], item[2]


def drop_staged_upload(token: str) -> None:
    with _lock:
        _staged.pop(token, None)


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


def mark_failed(job_id: str, error: str, error_code: str | None = None) -> Job | None:
    return update(
        job_id,
        status=JobStatus.FAILED,
        error=error,
        error_code=error_code,
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


def request_cancel(job_id: str) -> bool:
    """Signal a running job to stop. Returns True if the job was found and flagged."""
    with _lock:
        job = _jobs.get(job_id)
        if not job or job.is_terminal:
            return False
        job.cancel_requested = True
        return True


def clear_all() -> None:
    """Test helper: wipe the registry."""
    with _lock:
        _jobs.clear()
