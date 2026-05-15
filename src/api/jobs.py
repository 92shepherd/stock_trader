"""In-memory job registry for asynchronous collector runs.

The API distinguishes short and long collector calls:
  - Short (tickers, dart_corp_codes): run synchronously, return result
    directly in the HTTP response. No registry entry.
  - Long (daily_*, dart_disclosures, dart_financials, dart_indicators):
    return a job_id immediately, run in the background, expose status
    and result via GET /jobs/{id}.

That second path is what this module is for. The registry is
deliberately in-memory:
  - Restarting the API drops history (acceptable: collection_log on disk
    holds the durable truth — this registry is for "is the run I just
    kicked off still going?").
  - One process only — APScheduler and the API share it.
  - Bounded by `_MAX_JOBS` with FIFO eviction of finished entries.

Thread/async safety:
  All state changes go through `_lock` (asyncio.Lock). Job runs happen
  in a worker thread via `asyncio.to_thread(...)`; mutations from
  inside the worker call back into the registry via the async helpers.
"""
from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from src.api.locks import CollectorName
from src.utils.logger import logger


class JobStatus(str, Enum):
    """Lifecycle states for an async collector run."""

    PENDING = "pending"      # registered, not yet started
    RUNNING = "running"      # worker thread is executing
    SUCCESS = "success"      # finished, return value captured
    FAILED = "failed"        # exception raised, error string captured
    REJECTED = "rejected"    # never started (lock busy, validation, etc.)


# Keep at most this many entries. Old finished jobs roll off the back.
# Live (PENDING/RUNNING) jobs are never evicted regardless of count.
_MAX_JOBS = 200


class JobRecord:
    """One async collector run.

    The result is whatever the wrapped collector returned (typically the
    same dict the CLI prints — {ok, failed, ..., rows_inserted}). Kept
    as `dict | None` rather than typed to allow each collector its own
    shape without coupling this module to every signature.
    """

    __slots__ = (
        "id",
        "collector",
        "params",
        "status",
        "submitted_at",
        "started_at",
        "finished_at",
        "result",
        "error",
    )

    def __init__(
        self,
        *,
        job_id: str,
        collector: CollectorName,
        params: dict[str, Any],
    ) -> None:
        self.id = job_id
        self.collector = collector
        self.params = params
        self.status = JobStatus.PENDING
        self.submitted_at: datetime = datetime.now(timezone.utc)
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.result: dict[str, Any] | None = None
        self.error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Render as a JSON-friendly dict for HTTP responses."""
        return {
            "id": self.id,
            "collector": self.collector.value,
            "params": self.params,
            "status": self.status.value,
            "submitted_at": self.submitted_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": (
                self.finished_at.isoformat() if self.finished_at else None
            ),
            "result": self.result,
            "error": self.error,
        }


class JobRegistry:
    """Process-singleton registry. Use `get_registry()`."""

    def __init__(self) -> None:
        # OrderedDict gives O(1) FIFO eviction of finished jobs.
        self._jobs: OrderedDict[str, JobRecord] = OrderedDict()
        self._lock = asyncio.Lock()

    async def create(
        self,
        collector: CollectorName,
        params: dict[str, Any],
    ) -> JobRecord:
        """Allocate a new job_id in PENDING state and return the record."""
        job_id = uuid.uuid4().hex[:16]
        async with self._lock:
            self._evict_if_needed_locked()
            rec = JobRecord(job_id=job_id, collector=collector, params=params)
            self._jobs[job_id] = rec
        logger.debug(f"job created: {job_id} ({collector.value})")
        return rec

    async def mark_running(self, job_id: str) -> None:
        async with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                return
            rec.status = JobStatus.RUNNING
            rec.started_at = datetime.now(timezone.utc)

    async def mark_success(self, job_id: str, result: dict[str, Any] | None) -> None:
        async with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                return
            rec.status = JobStatus.SUCCESS
            rec.finished_at = datetime.now(timezone.utc)
            rec.result = result

    async def mark_failed(self, job_id: str, error: str) -> None:
        async with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                return
            rec.status = JobStatus.FAILED
            rec.finished_at = datetime.now(timezone.utc)
            # Truncate to keep responses bounded. Full traceback is in logs.
            rec.error = error[:2000]

    async def mark_rejected(self, job_id: str, reason: str) -> None:
        async with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                return
            rec.status = JobStatus.REJECTED
            rec.finished_at = datetime.now(timezone.utc)
            rec.error = reason[:2000]

    async def get(self, job_id: str) -> JobRecord | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def list_all(
        self,
        *,
        collector: CollectorName | None = None,
        status: JobStatus | None = None,
        limit: int = 50,
    ) -> list[JobRecord]:
        """Most-recently-submitted first. Optionally filter by collector/status."""
        async with self._lock:
            items = list(self._jobs.values())
        items.sort(key=lambda r: r.submitted_at, reverse=True)
        if collector is not None:
            items = [r for r in items if r.collector == collector]
        if status is not None:
            items = [r for r in items if r.status == status]
        return items[: max(1, min(limit, _MAX_JOBS))]

    def _evict_if_needed_locked(self) -> None:
        """Drop the oldest finished job(s) until size <= _MAX_JOBS - 1.

        MUST be called while holding `self._lock`. Live jobs (PENDING /
        RUNNING) are skipped during eviction so we never delete a job
        that's still in flight.
        """
        if len(self._jobs) < _MAX_JOBS:
            return
        # Walk insertion order, delete first FINISHED entry we find.
        for jid, rec in list(self._jobs.items()):
            if rec.status in (JobStatus.PENDING, JobStatus.RUNNING):
                continue
            del self._jobs[jid]
            if len(self._jobs) < _MAX_JOBS:
                return


# Process-singleton accessor.
_registry: JobRegistry | None = None


def get_registry() -> JobRegistry:
    global _registry
    if _registry is None:
        _registry = JobRegistry()
    return _registry


__all__ = [
    "JobRegistry",
    "JobRecord",
    "JobStatus",
    "get_registry",
]
