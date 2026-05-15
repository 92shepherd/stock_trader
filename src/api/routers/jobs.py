"""Job tracking endpoints \u2014 list and inspect async collector runs.

Pairs with `src.api.jobs.JobRegistry`. Long-running collector triggers
(daily_*, dart_disclosures, dart_financials, dart_indicators) return a
`job_id` immediately; clients poll here for progress.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.api.auth import require_api_key
from src.api.jobs import JobStatus, get_registry
from src.api.locks import CollectorName
from src.api.schemas import (
    JobListResponse,
    JobStatusResponse,
)

router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_api_key)],
)


def _to_response(record) -> JobStatusResponse:
    """Map JobRecord \u2192 JobStatusResponse without importing the schemas
    on the hot path of `mark_*` calls."""
    return JobStatusResponse(**record.to_dict())


@router.get(
    "",
    response_model=JobListResponse,
    summary="List recent async collector runs",
)
async def list_jobs(
    collector: str | None = Query(
        None,
        description=(
            "Filter by collector name (e.g. 'daily_kis'). "
            "Unknown names return an empty list."
        ),
    ),
    status_filter: str | None = Query(
        None,
        alias="status",
        description="Filter by status: pending|running|success|failed|rejected.",
    ),
    limit: int = Query(50, ge=1, le=200),
) -> JobListResponse:
    """Most-recently-submitted first."""
    registry = get_registry()

    # Validate filters \u2014 silently coerce so a typo gives an empty list
    # rather than a 500. (CollectorName/JobStatus values are stable
    # short strings, so the contract is easy for clients.)
    coll_enum: CollectorName | None = None
    if collector:
        try:
            coll_enum = CollectorName(collector)
        except ValueError:
            return JobListResponse(jobs=[])

    status_enum: JobStatus | None = None
    if status_filter:
        try:
            status_enum = JobStatus(status_filter)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Invalid status; must be one of: "
                    "pending, running, success, failed, rejected."
                ),
            )

    records = await registry.list_all(
        collector=coll_enum,
        status=status_enum,
        limit=limit,
    )
    return JobListResponse(jobs=[_to_response(r) for r in records])


@router.get(
    "/{job_id}",
    response_model=JobStatusResponse,
    summary="Get one job by id",
)
async def get_job(job_id: str) -> JobStatusResponse:
    record = await get_registry().get(job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown job_id: {job_id!r}",
        )
    return _to_response(record)
