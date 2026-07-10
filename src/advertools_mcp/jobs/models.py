"""Job record data model, serialisable to JSON for persistence."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {JobState.DONE, JobState.FAILED, JobState.CANCELLED}


class JobKind(str, Enum):
    CRAWL = "crawl"
    HEADER_CRAWL = "header_crawl"
    IMAGE_CRAWL = "image_crawl"


def _new_id(kind: JobKind) -> str:
    return f"{kind.value}-{uuid.uuid4().hex[:12]}"


@dataclass
class JobRecord:
    """A single crawl job. Persisted as one JSON file under ``data_dir/_jobs``."""

    job_id: str
    kind: str
    state: str = JobState.QUEUED.value
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    # Resolved & sanitised request parameters (echoed back to the user).
    params: dict[str, Any] = field(default_factory=dict)

    # Output artefacts on disk.
    output_jl: str | None = None
    output_parquet: str | None = None
    output_dir: str | None = None  # image crawls write a directory

    # Progress / results.
    pages: int = 0
    errors: int = 0
    error_message: str | None = None
    finish_reason: str | None = None
    pid: int | None = None

    @classmethod
    def create(cls, kind: JobKind, params: dict[str, Any]) -> "JobRecord":
        return cls(job_id=_new_id(kind), kind=kind.value, params=params)

    @property
    def runtime_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.time()
        return round(end - self.started_at, 3)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["runtime_seconds"] = self.runtime_seconds
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobRecord":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})
