"""Async audit jobs.

Audits can be slow on large datasets (hundreds of URLs, plus live robots/sitemap
fetches and optional CWV/render passes), so — like crawls — they run in the
background and are polled, instead of blocking the tool call until done. Unlike
crawls, audits don't touch the Twisted reactor, so they run in a worker thread
rather than a subprocess. State is persisted to the shared JobStore so it
survives a client reconnect.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from ..config import Settings
from .models import JobKind, JobRecord, JobState
from .store import JobStore


class AuditManager:
    def __init__(self, settings: Settings, store: JobStore):
        self.settings = settings
        self.store = store
        self._sem = asyncio.Semaphore(max(1, settings.max_concurrent_jobs))
        self._tasks: dict[str, asyncio.Task] = {}

    def submit(self, kind: JobKind, params: dict[str, Any], run: Callable[[], dict]) -> JobRecord:
        """Register an audit job and launch it in the background. Returns at once."""
        record = JobRecord.create(kind, params)
        self.store.save(record)
        task = asyncio.create_task(self._run(record.job_id, run))
        self._tasks[record.job_id] = task
        return record

    async def _run(self, job_id: str, run: Callable[[], dict]) -> None:
        async with self._sem:
            record = self.store.get(job_id)
            if record is None or JobState(record.state).terminal:
                return
            record.state = JobState.RUNNING.value
            record.started_at = time.time()
            self.store.save(record)
            try:
                result = await asyncio.to_thread(run)
            except asyncio.CancelledError:
                self._finalise(job_id, JobState.CANCELLED, finish_reason="cancelled")
                raise
            except Exception as exc:  # noqa: BLE001 - report any audit failure to the client
                self._finalise(
                    job_id, JobState.FAILED,
                    error_message=f"{type(exc).__name__}: {exc}", finish_reason="exception",
                )
                return
            self._finalise(job_id, JobState.DONE, result=result, finish_reason="finished")

    def _finalise(self, job_id, state, *, result=None, error_message=None, finish_reason=None):
        record = self.store.get(job_id)
        if record is None or record.state == JobState.CANCELLED.value:
            return
        record.state = state.value
        record.finished_at = time.time()
        record.result = result
        record.error_message = error_message
        record.finish_reason = finish_reason
        self.store.save(record)

    def status(self, job_id: str) -> JobRecord | None:
        return self.store.get(job_id)

    async def cancel(self, job_id: str) -> bool:
        record = self.store.get(job_id)
        if record is None or JobState(record.state).terminal:
            return False
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        else:
            self._finalise(job_id, JobState.CANCELLED, finish_reason="cancelled")
        return True
