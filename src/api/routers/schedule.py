"""Scheduler management endpoints.

Read-only `GET /schedule` for inspection; `POST /schedule/{id}/pause`
and `POST /schedule/{id}/resume` for manual control. Reconfiguring the
cron expression at runtime is intentionally NOT exposed \u2014 keep that in
.env where it survives restarts and lives next to the rest of config.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from src.api.auth import require_api_key
from src.api.schemas import (
    ScheduleEntry,
    ScheduleListResponse,
    SchedulePauseResponse,
)

router = APIRouter(
    prefix="/schedule",
    tags=["schedule"],
    dependencies=[Depends(require_api_key)],
)


def _get_scheduler(request: Request):
    """Pull the scheduler off app.state. None when disabled via env."""
    return getattr(request.app.state, "scheduler", None)


@router.get(
    "",
    response_model=ScheduleListResponse,
    summary="List all scheduled jobs and their next fire times",
)
async def list_schedules(request: Request) -> ScheduleListResponse:
    scheduler = _get_scheduler(request)
    if scheduler is None:
        # Scheduler disabled in env. Return empty list rather than 503
        # so clients can distinguish "off by config" from "broken".
        import os
        return ScheduleListResponse(
            enabled=False,
            timezone=os.getenv("SCHEDULER_TIMEZONE", "Asia/Seoul"),
            schedules=[],
        )

    entries: list[ScheduleEntry] = []
    for job in scheduler.get_jobs():
        entries.append(
            ScheduleEntry(
                id=job.id,
                name=job.name or job.id,
                next_run_time=(
                    job.next_run_time.isoformat() if job.next_run_time else None
                ),
                trigger=str(job.trigger),
            )
        )
    return ScheduleListResponse(
        enabled=True,
        timezone=str(scheduler.timezone),
        schedules=entries,
    )


@router.post(
    "/{job_id}/pause",
    response_model=SchedulePauseResponse,
    summary="Pause a scheduled job (does not affect in-flight runs)",
)
async def pause_schedule(job_id: str, request: Request) -> SchedulePauseResponse:
    scheduler = _get_scheduler(request)
    if scheduler is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Scheduler is disabled (SCHEDULER_ENABLED=false).",
        )
    if scheduler.get_job(job_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown schedule id: {job_id!r}",
        )
    scheduler.pause_job(job_id)
    return SchedulePauseResponse(id=job_id, paused=True)


@router.post(
    "/{job_id}/resume",
    response_model=SchedulePauseResponse,
    summary="Resume a paused scheduled job",
)
async def resume_schedule(job_id: str, request: Request) -> SchedulePauseResponse:
    scheduler = _get_scheduler(request)
    if scheduler is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Scheduler is disabled (SCHEDULER_ENABLED=false).",
        )
    if scheduler.get_job(job_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown schedule id: {job_id!r}",
        )
    scheduler.resume_job(job_id)
    return SchedulePauseResponse(id=job_id, paused=False)
