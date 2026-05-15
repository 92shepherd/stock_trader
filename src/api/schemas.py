"""Pydantic request/response models for the API.

Naming: every request model ends in `Request`, response model in
`Response`. Field names match each collector's keyword arguments
verbatim so the API surface mirrors the CLI flags one-for-one (e.g.
`start_date`, `end_date`, `skip_done`).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Generic envelopes
# ---------------------------------------------------------------------------


class JobAcceptedResponse(BaseModel):
    """Returned for async POSTs \u2014 client polls /jobs/{id} afterwards."""

    job_id: str = Field(..., description="Opaque identifier for GET /jobs/{id}")
    collector: str
    status: Literal["pending", "running"] = "pending"
    submitted_at: str = Field(..., description="ISO-8601 UTC")


class SyncRunResponse(BaseModel):
    """Returned for sync POSTs that complete in-band."""

    collector: str
    status: Literal["success", "failed"]
    duration_ms: int
    result: dict[str, Any] | None = None
    error: str | None = None


class JobStatusResponse(BaseModel):
    """One job's full state \u2014 returned by GET /jobs/{id}."""

    id: str
    collector: str
    params: dict[str, Any]
    status: Literal["pending", "running", "success", "failed", "rejected"]
    submitted_at: str
    started_at: str | None
    finished_at: str | None
    result: dict[str, Any] | None
    error: str | None


class JobListResponse(BaseModel):
    jobs: list[JobStatusResponse]


# ---------------------------------------------------------------------------
# Master-data collectors (synchronous \u2014 fast)
# ---------------------------------------------------------------------------


class TickersKRRequest(BaseModel):
    """POST /collect/tickers/kr"""

    desc: bool = Field(
        True,
        description=(
            "If true (default), use FDR's '-DESC' listing variant to also "
            "populate sector/industry/listing_date. Slightly slower."
        ),
    )


class TickersUSRequest(BaseModel):
    """POST /collect/tickers/us \u2014 no body needed; placeholder for symmetry."""

    pass


class DartCorpCodesRequest(BaseModel):
    """POST /collect/dart/corp-codes"""

    force: bool = Field(
        False,
        description=(
            "Bypass the freshness check and re-download even if the local "
            "cache is younger than `stale_after_days`."
        ),
    )
    stale_after_days: int | None = Field(
        None,
        ge=1,
        le=365,
        description=(
            "Override `cfg.dart.corp_codes_stale_after_days`. Refresh only "
            "if the table is older than this many days."
        ),
    )


# ---------------------------------------------------------------------------
# Daily-price collectors (async \u2014 long-running)
# ---------------------------------------------------------------------------


class _DateRangeMixin(BaseModel):
    """Shared start/end/days fields with sanity checks.

    Exactly the same semantics the CLI pipelines use:
      - `days` is a backfill window in CALENDAR days ending at end_date.
      - If `start_date` is given, `days` is ignored.
      - `end_date` defaults to today inside the collector.
    """

    start_date: date | None = None
    end_date: date | None = None
    days: int | None = Field(None, ge=1, le=10_000)

    @field_validator("end_date")
    @classmethod
    def _end_after_start(cls, v: date | None, info) -> date | None:
        start = info.data.get("start_date")
        if v is not None and start is not None and v < start:
            raise ValueError("end_date must be on or after start_date")
        return v


class DailyFDRRequest(_DateRangeMixin):
    """POST /collect/daily/fdr"""

    symbols: list[str] | None = Field(
        None,
        description=(
            "Pinned 6-digit Korean symbols. None = full active universe "
            "from the `tickers` table."
        ),
    )
    markets: list[str] | None = Field(
        None,
        description="e.g. ['KOSPI', 'KOSDAQ']. None = from settings.yaml.",
    )
    skip_done: bool = True

    @field_validator("symbols")
    @classmethod
    def _zero_pad(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return [s.strip().zfill(6) for s in v if s and s.strip()]


class DailyKISRequest(_DateRangeMixin):
    """POST /collect/daily/kis"""

    symbols: list[str] | None = None
    skip_done: bool = True
    fetch_snapshot: bool = Field(
        True,
        description=(
            "If true (default), call inquire-price per symbol to populate "
            "market_cap/PER/PBR/foreign_net on the end_date row."
        ),
    )
    request_delay: float | None = Field(
        None,
        ge=0.0,
        le=5.0,
        description=(
            "Seconds between KIS API calls. None = mode-default (real: "
            "0.07s, paper: 0.25s)."
        ),
    )

    @field_validator("symbols")
    @classmethod
    def _zero_pad(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return [s.strip().zfill(6) for s in v if s and s.strip()]


class DailyUSRequest(_DateRangeMixin):
    """POST /collect/daily/us"""

    symbols: list[str] | None = Field(
        None,
        description="Pinned US tickers (e.g. ['AAPL', 'BRK-B']).",
    )
    exchanges: list[str] | None = None
    security_types: list[str] | None = None
    skip_done: bool = True


# ---------------------------------------------------------------------------
# DART collectors (mixed sync/async)
# ---------------------------------------------------------------------------


class DartDisclosuresRequest(_DateRangeMixin):
    """POST /collect/dart/disclosures

    Date-iterating. Async because a wide kinds set + multi-day window
    can take many minutes.
    """

    kinds: list[str] | None = Field(
        None,
        description=(
            "DART kind codes. None = ('B',) (major events). 'ALL' as a "
            "single-element list expands to every kind."
        ),
    )
    listed_only: bool = True
    skip_done: bool = True


class DartFinancialsRequest(BaseModel):
    """POST /collect/dart/financials \u2014 the heavy-hitter.

    A full 2020+ backfill is ~120k API calls (12 days at the personal
    tier). The API mirrors the CLI: use `max_calls` to fit within a
    daily budget and re-trigger tomorrow.
    """

    start_year: int = Field(2020, ge=2000, le=2100)
    end_year: int | None = Field(None, ge=2000, le=2100)
    reprt_codes: list[str] | None = Field(
        None,
        description="['11013'=Q1, '11012'=H1, '11014'=Q3, '11011'=FY]. None=all.",
    )
    fs_divs: list[Literal["CFS", "OFS"]] | None = None
    corp_codes: list[str] | None = Field(
        None,
        description="Explicit DART corp_code list. None = all listed.",
    )
    skip_done: bool = True
    max_calls: int | None = Field(
        None,
        ge=1,
        le=20_000,
        description="Hard cap on API calls this run. None = no limit.",
    )

    @field_validator("end_year")
    @classmethod
    def _end_year_after_start(cls, v: int | None, info) -> int | None:
        s = info.data.get("start_year")
        if v is not None and s is not None and v < s:
            raise ValueError("end_year must be >= start_year")
        return v


class DartIndicatorsRequest(DartFinancialsRequest):
    """POST /collect/dart/indicators \u2014 same shape as financials plus idx_cl."""

    idx_cl_codes: list[str] | None = Field(
        None,
        description=(
            "Indicator class codes (M210000/M220000/M230000/M240000). "
            "None = all 4."
        ),
    )


# ---------------------------------------------------------------------------
# Composite: the default daily cron (POST /collect/daily-cron)
# ---------------------------------------------------------------------------


class DailyCronRequest(BaseModel):
    """POST /collect/daily-cron \u2014 KIS daily + DART disclosures.

    Re-implements `python -m src.main`'s logic as an HTTP-triggerable
    job. Same defaults: yesterday, 1-day window, snapshot on.
    """

    end_date: date | None = Field(
        None,
        description=(
            "Target end date. None = yesterday (matches the 03:00 cron's "
            "'last completed trading day' assumption)."
        ),
    )
    days: int = Field(1, ge=1, le=30)
    only: Literal["kis", "dart"] | None = None
    fetch_snapshot: bool = True
    skip_done: bool = True


# ---------------------------------------------------------------------------
# Scheduler management
# ---------------------------------------------------------------------------


class ScheduleEntry(BaseModel):
    id: str
    name: str
    next_run_time: str | None
    trigger: str


class ScheduleListResponse(BaseModel):
    enabled: bool
    timezone: str
    schedules: list[ScheduleEntry]


class SchedulePauseResponse(BaseModel):
    id: str
    paused: bool


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    busy_collectors: list[str]
    scheduler_running: bool
    db_ok: bool


__all__ = [
    # generic
    "JobAcceptedResponse",
    "SyncRunResponse",
    "JobStatusResponse",
    "JobListResponse",
    # requests
    "TickersKRRequest",
    "TickersUSRequest",
    "DartCorpCodesRequest",
    "DailyFDRRequest",
    "DailyKISRequest",
    "DailyUSRequest",
    "DartDisclosuresRequest",
    "DartFinancialsRequest",
    "DartIndicatorsRequest",
    "DailyCronRequest",
    # scheduler
    "ScheduleEntry",
    "ScheduleListResponse",
    "SchedulePauseResponse",
    # health
    "HealthResponse",
]
