"""The book history: every upload or Drive file and how it went.

Kept in memory and, when given a file, saved to it after every change so it survives
restarts. Entries older than the retention period are dropped.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

MAX_JOBS = 1000
FINAL = {"done", "failed"}


@dataclass
class Job:
    id: str
    filename: str
    source: str = "upload"  # upload | drive
    status: str = "queued"  # queued | fulfilling | removing_drm | sending | done | failed
    title: str | None = None
    error: str | None = None
    book: str | None = None  # file name in the books folder, once converted
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class JobStore:
    def __init__(self, path: Path | None = None, retention_days: int = 30) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.path = path
        self.retention_days = retention_days
        if path:
            self._load()

    def create(self, filename: str, source: str = "upload") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], filename=filename, source=source)
        with self._lock:
            self._jobs[job.id] = job
            self._trim()
            self._save()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job: Job, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated = time.time()
            self._save()

    def recent(self, limit: int = 10) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)[:limit]

    def prune(self) -> None:
        with self._lock:
            if self._trim():
                self._save()

    # --- internals (call with the lock held)

    def _trim(self) -> bool:
        cutoff = time.time() - self.retention_days * 86400
        old = sorted(self._jobs.values(), key=lambda j: j.created)
        drop = [j for j in old if j.created < cutoff and j.status in FINAL]
        drop += [j for j in old[: max(0, len(old) - MAX_JOBS)] if j not in drop]
        for job in drop:
            del self._jobs[job.id]
        return bool(drop)

    def _load(self) -> None:
        try:
            rows = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except ValueError:
            log.warning("History file %s is damaged; starting a new one", self.path)
            return
        names = {f.name for f in fields(Job)}
        for row in rows:
            job = Job(**{k: v for k, v in row.items() if k in names})
            if job.status not in FINAL:
                job.status, job.error = "failed", "Interrupted because the service restarted. Please try again."
            self._jobs[job.id] = job
        self._trim()

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps([j.to_dict() for j in self._jobs.values()], ensure_ascii=False))
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Couldn't save the history to %s", self.path)
