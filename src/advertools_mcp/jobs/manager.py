"""Async job manager: spawns one subprocess per crawl, capped by a semaphore.

The server stays responsive because every crawl is offloaded to a child process
(:mod:`advertools_mcp.workers.crawl_worker`) and supervised by a background
asyncio task. Tools call :meth:`submit` and get a ``job_id`` back immediately.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from .models import JobKind, JobRecord, JobState
from .store import JobStore


class JobManager:
    def __init__(self, settings: Settings, store: JobStore):
        self.settings = settings
        self.store = store
        self._sem = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._tasks: dict[str, asyncio.Task] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    # ------------------------------------------------------------------ submit
    def submit(self, kind: JobKind, params: dict[str, Any], spec: dict[str, Any]) -> JobRecord:
        """Register a job and launch its supervisor task. Returns immediately."""
        record = JobRecord.create(kind, params)
        crawl_dir = self.settings.data_dir / record.job_id
        crawl_dir.mkdir(parents=True, exist_ok=True)

        record.output_jl = str(crawl_dir / "crawl.jl")
        if kind == JobKind.IMAGE_CRAWL:
            record.output_dir = str(crawl_dir / "images")
            Path(record.output_dir).mkdir(parents=True, exist_ok=True)
        record.output_parquet = str(crawl_dir / "crawl.parquet")

        spec.update(
            {
                "job_id": record.job_id,
                "kind": kind.value,
                "output_jl": record.output_jl,
                "output_parquet": record.output_parquet,
                "output_dir": record.output_dir,
                "result_path": str(crawl_dir / "result.json"),
            }
        )
        spec_path = crawl_dir / "spec.json"
        spec_path.write_text(json.dumps(spec, indent=2, default=str))

        self.store.save(record)
        task = asyncio.create_task(self._supervise(record.job_id, str(spec_path)))
        self._tasks[record.job_id] = task
        return record

    # --------------------------------------------------------------- supervise
    async def _supervise(self, job_id: str, spec_path: str) -> None:
        async with self._sem:
            record = self.store.get(job_id)
            if record is None or record.state == JobState.CANCELLED.value:
                return
            record.state = JobState.RUNNING.value
            record.started_at = time.time()
            self.store.save(record)

            spec = json.loads(Path(spec_path).read_text())
            result_path = spec["result_path"]
            if os.path.exists(result_path):
                os.unlink(result_path)

            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "advertools_mcp.workers.crawl_worker",
                    spec_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            except Exception as exc:  # noqa: BLE001
                self._finalise_failed(job_id, f"Failed to spawn worker: {exc}")
                return

            self._procs[job_id] = proc
            record.pid = proc.pid
            self.store.save(record)

            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.settings.max_job_runtime_seconds
                )
            except asyncio.TimeoutError:
                self._kill(proc)
                self._finalise_failed(
                    job_id,
                    f"Exceeded max runtime ({self.settings.max_job_runtime_seconds}s).",
                    finish_reason="timeout",
                )
                return
            except asyncio.CancelledError:
                self._kill(proc)
                self._finalise_cancelled(job_id)
                raise
            finally:
                self._procs.pop(job_id, None)

            self._finalise_from_result(job_id, result_path, proc.returncode, stderr)

    # ------------------------------------------------------------- finalisers
    def _finalise_from_result(
        self, job_id: str, result_path: str, returncode: int | None, stderr: bytes | None
    ) -> None:
        record = self.store.get(job_id)
        if record is None or record.state == JobState.CANCELLED.value:
            return
        result: dict[str, Any] = {}
        if os.path.exists(result_path):
            try:
                result = json.loads(Path(result_path).read_text())
            except json.JSONDecodeError:
                result = {}

        record.finished_at = time.time()
        record.pages = int(result.get("pages", record.pages) or 0)
        record.errors = int(result.get("errors", 0) or 0)
        record.finish_reason = result.get("finish_reason")
        if result.get("output_parquet"):
            record.output_parquet = result["output_parquet"]

        if result.get("state") == "done":
            record.state = JobState.DONE.value
        else:
            record.state = JobState.FAILED.value
            msg = result.get("error_message")
            if not msg:
                tail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
                msg = tail[-1] if tail else f"Worker exited with code {returncode}."
            record.error_message = msg
        self.store.save(record)

    def _finalise_failed(self, job_id: str, message: str, finish_reason: str = "exception") -> None:
        record = self.store.get(job_id)
        if record is None:
            return
        record.state = JobState.FAILED.value
        record.error_message = message
        record.finish_reason = finish_reason
        record.finished_at = time.time()
        self.store.save(record)

    def _finalise_cancelled(self, job_id: str) -> None:
        record = self.store.get(job_id)
        if record is None:
            return
        record.state = JobState.CANCELLED.value
        record.finish_reason = "cancelled"
        record.finished_at = time.time()
        self.store.save(record)

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return

    # ------------------------------------------------------------------- query
    def status(self, job_id: str) -> JobRecord | None:
        record = self.store.get(job_id)
        if record is None:
            return None
        # Live page count for a running crawl: advertools writes .jl incrementally.
        if record.state == JobState.RUNNING.value and record.output_jl:
            try:
                with open(record.output_jl, "rb") as fh:
                    record.pages = sum(1 for _ in fh)
            except OSError:
                pass
        return record

    def list(self) -> list[JobRecord]:
        return self.store.list()

    async def cancel(self, job_id: str) -> bool:
        record = self.store.get(job_id)
        if record is None or JobState(record.state).terminal:
            return False
        task = self._tasks.get(job_id)
        proc = self._procs.get(job_id)
        if proc is not None:
            self._kill(proc)
        if task is not None and not task.done():
            task.cancel()
        else:
            self._finalise_cancelled(job_id)
        return True
