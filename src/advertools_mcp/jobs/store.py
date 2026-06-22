"""Filesystem-backed job store.

Each job is a single JSON file (``<job_id>.json``) under ``data_dir/_jobs`` so
state survives a client reconnect or a server restart, and so the worker
subprocess and the server can communicate through a stable artefact path.
Writes are atomic (write-temp-then-rename).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .models import JobRecord, JobState


class JobStore:
    def __init__(self, jobs_dir: Path):
        self.jobs_dir = jobs_dir
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def save(self, record: JobRecord) -> None:
        path = self._path(record.job_id)
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=self.jobs_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(record.to_dict(), fh, indent=2, default=str)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    def get(self, job_id: str) -> JobRecord | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        with path.open() as fh:
            return JobRecord.from_dict(json.load(fh))

    def list(self) -> list[JobRecord]:
        records: list[JobRecord] = []
        for path in self.jobs_dir.glob("*.json"):
            try:
                with path.open() as fh:
                    records.append(JobRecord.from_dict(json.load(fh)))
            except (json.JSONDecodeError, OSError):
                continue
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    def reconcile_orphans(self) -> int:
        """Mark jobs left ``running``/``queued`` by a prior process as failed.

        Called once on startup: an asyncio task no longer exists for these jobs,
        so they can never complete. Their partial artefacts (if any) are kept.
        """
        count = 0
        for record in self.list():
            if record.state in {JobState.RUNNING.value, JobState.QUEUED.value}:
                record.state = JobState.FAILED.value
                record.error_message = "Interrupted by server restart."
                record.finish_reason = "server_restart"
                self.save(record)
                count += 1
        return count
