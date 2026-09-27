"""In-memory job log. One user, one process: nothing here needs to survive a restart."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

MAX_JOBS = 50


@dataclass
class Job:
    id: str
    filename: str
    source: str = "upload"  # upload | drive
    status: str = "queued"  # queued | fulfilling | removing_drm | sending | done | failed
    title: str | None = None
    error: str | None = None
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, filename: str, source: str = "upload") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], filename=filename, source=source)
        with self._lock:
            self._jobs[job.id] = job
            if len(self._jobs) > MAX_JOBS:
                oldest = sorted(self._jobs.values(), key=lambda j: j.created)
                for old in oldest[: len(self._jobs) - MAX_JOBS]:
                    del self._jobs[old.id]
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job: Job, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated = time.time()

    def recent(self, limit: int = 10) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)[:limit]
