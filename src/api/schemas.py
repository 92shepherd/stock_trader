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


class MinuteKISRequest(BaseModel):
    """POST /collect/minute/kis

    KIS 1분봉 수집. 조회 가능 기간: 최근 30거래일.

    성능 주의:
        하루치 1종목 수집에 ~13번의 API 호출이 필요하며,
        target_date 가 오래될수록 중간 날짜를 건너뛰는 호출이 추가됨.
        전체 활성 종목(~2,600개) × 30일 수집은 수십 시간 소요.
        대상 종목을 symbols 로 명시하거나 force_full_universe=true 를 명시적으로
        설정하도록 설계되어 있음.
    """

    symbols: list[str] | None = Field(
        None,
        description=(
            "6자리 종목코드 리스트. None = 전체 활성 종목 (force_full_universe=true 필요)."
        ),
    )
    start_date: date | None = Field(
        None,
        description="수집 시작일. None = 30거래일 전 (KIS API 최대 기간).",
    )
    end_date: date | None = Field(
        None,
        description="수집 종료일. None = 오늘.",
    )
    skip_done: bool = Field(
        True,
        description="collection_log 에 이미 성공한 (symbol, date) 쌍 스킵.",
    )
    request_delay: float | None = Field(
        None,
        ge=0.0,
        le=5.0,
        description="API 호출 간격(초). None = 모드 기본값 (real: 0.07s, paper: 0.25s).",
    )
    force_full_universe: bool = Field(
        False,
        description=(
            "symbols=None 일 때 전체 활성 종목 수집 허용. "
            "소요 시간이 매우 길 수 있으므로 명시적으로 활성화해야 함."
        ),
    )

    @field_validator("symbols")
    @classmethod
    def _zero_pad(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return [s.strip().zfill(6) for s in v if s and s.strip()]

    @field_validator("end_date")
    @classmethod
    def _end_after_start(cls, v: date | None, info) -> date | None:
        start = info.data.get("start_date")
        if v is not None and start is not None and v < start:
            raise ValueError("end_date must be on or after start_date")
        return v


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
# Consensus collectors (async \u2014 long-running)
# ---------------------------------------------------------------------------


class ConsensusFnguideRequest(BaseModel):
    """POST /collect/consensus/fnguide — FnGuide 일 스냅샷 (개인용).

    Date model:
        `as_of_date` 가 스냅샷 날짜. None 이면 수집기가 오늘로
        결정. 같은 날짜를 다시 상단해도 PK 잠금 (symbol, as_of_date,
        fiscal_period) 로 멱등성 보장.

    사전 조건:
        .env 에 FNGUIDE_CONSENT_ACK=1 이 설정되어 있어야 함. 없으면
        제출된 job 은 'failed' 로 마킹되며 error 필드에 안내 문구가 남는다.
    """

    symbols: list[str] | None = Field(
        None,
        description=(
            "특정 6자리 종목코드 리스트. None 이면 `tickers` 테이블의 전테 활성 종목."
        ),
    )
    as_of_date: date | None = Field(
        None,
        description="스냅샷 날짜 (YYYY-MM-DD). None = 오늘.",
    )
    markets: list[str] | None = Field(
        None,
        description="전 종목 모드에서 대상 시장. 예: ['KOSPI', 'KOSDAQ']. None = config.",
    )
    skip_done: bool = True
    request_delay: float | None = Field(
        None,
        ge=0.0,
        le=10.0,
        description=(
            "요청 간 sleep (초). None = max(cfg.daily.request_delay, 1.0). "
            "FnGuide 서버 매너 차원에서 최소 1이 권장됨."
        ),
    )

    @field_validator("symbols")
    @classmethod
    def _zero_pad(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return [s.strip().zfill(6) for s in v if s and s.strip()]


class ConsensusHankyungRequest(BaseModel):
    """POST /collect/consensus/hankyung — 한경 컨센서스 리포트 수집.

    모드 우선순위:
        1. backfill_year=True  → 오늘 기준 1년치 백필
        2. from_date / to_date → 날짜 범위 수집
        3. target_date         → 하루 수집
        4. 모두 None           → 오늘 하루 수집

    사전 조건:
        HANKYUNG_ENABLED=true 가 환경변수에 설정되어 있어야 함.
        docker-compose.gcp-db.yml 실행 시에만 활성화됨.
    """

    target_date: date | None = Field(
        None,
        description="하루 수집 대상일 (YYYY-MM-DD). None 이면 오늘.",
    )
    from_date: date | None = Field(
        None,
        description="날짜 범위 수집 시작일. to_date 와 함께 사용.",
    )
    to_date: date | None = Field(
        None,
        description="날짜 범위 수집 종료일. from_date 와 함께 사용.",
    )
    backfill_year: bool = Field(
        False,
        description="True 이면 오늘(to_date) 기준 1년치 백필. 다른 날짜 파라미터보다 우선.",
    )
    skip_done: bool = True
    request_delay: float | None = Field(
        None,
        ge=0.0,
        le=10.0,
        description="페이지 요청 간 sleep (초). None = 0.5s.",
    )


# ---------------------------------------------------------------------------
# Research: factor evaluation (POST /research/factor/evaluate)
# ---------------------------------------------------------------------------


class FactorEvalRequest(BaseModel):
    """POST /research/factor/evaluate — 단일 팩터 평가.

    모드:
        factor_name 을 하나 지정하면 해당 팩터 단독 평가.

    기간 기본값:
        start_date = 오늘 기준 1년 전, end_date = 어제.
    """

    factor_name: str = Field(
        ...,
        description=(
            "평가할 팩터 이름. 예: momentum_20d, pead_revenue_yoy, rev_eps_1m_fy1. "
            "BASELINE_FACTORS / PEAD_FACTORS / RATING_FACTORS / REVISION_FACTORS / QUALITY_FACTORS "
            "에 등록된 이름이어야 함."
        ),
    )
    universe: str = Field("ALL", description="평가 universe. ALL / KOSPI / KOSDAQ / KOSPI200.")
    start_date: date | None = Field(None, description="평가 시작일. None 이면 오늘 기준 1년 전.")
    end_date: date | None = Field(None, description="평가 종료일. None 이면 어제.")
    horizon_days: int = Field(5, ge=1, le=60, description="forward return horizon (일).")
    persist_signals: bool = Field(False, description="팩터 신호를 factor_signals 에 저장할지 여부.")
    persist_run: bool = Field(True, description="평가 결과를 eval_runs / eval_metrics 에 저장할지 여부.")


class FactorEvalAllRequest(BaseModel):
    """POST /research/factor/evaluate/all — 전체(또는 지정) 팩터 일괄 평가.

    factors 를 지정하지 않으면 데이터가 있는 모든 팩터를 순서대로 평가.
    개별 팩터 에러는 건너뛰고 나머지를 계속 진행함.
    """

    universe: str = Field("ALL", description="평가 universe.")
    start_date: date | None = Field(None, description="평가 시작일. None 이면 오늘 기준 1년 전.")
    end_date: date | None = Field(None, description="평가 종료일. None 이면 어제.")
    horizon_days: int = Field(5, ge=1, le=60, description="forward return horizon (일).")
    factors: list[str] | None = Field(
        None,
        description=(
            "평가할 팩터 이름 목록. None 이면 전체 팩터 레지스트리에서 순서대로 실행. "
            "예: [\"momentum_20d\", \"pead_revenue_yoy\"]"
        ),
    )
    persist_signals: bool = Field(False, description="팩터 신호를 factor_signals 에 저장할지 여부.")
    persist_run: bool = Field(True, description="평가 결과를 eval_runs / eval_metrics 에 저장할지 여부.")


# ---------------------------------------------------------------------------
# Composite: the default daily cron (POST /collect/daily-cron)
# ---------------------------------------------------------------------------


class DailyCronRequest(BaseModel):
    """POST /collect/daily-cron \u2014 KIS daily + DART + FnGuide consensus.

    Re-implements `python -m src.main`'s logic as an HTTP-triggerable
    job, extended with the FnGuide consensus snapshot step. Same
    defaults: yesterday, 1-day window, snapshot on.

    Note on FnGuide step:
        Runs only if .env has FNGUIDE_CONSENT_ACK=1. Without consent
        the step is silently skipped (summary['fnguide_consensus'] =
        'skipped_no_consent') — not treated as a failure.
    """

    end_date: date | None = Field(
        None,
        description=(
            "Target end date. None = yesterday (matches the 03:00 cron's "
            "'last completed trading day' assumption)."
        ),
    )
    days: int = Field(1, ge=1, le=30)
    only: Literal["kis", "dart", "fnguide"] | None = Field(
        None,
        description=(
            "Narrow to a single step. None = run all three (KIS + DART + "
            "FnGuide consensus)."
        ),
    )
    fetch_snapshot: bool = True
    skip_done: bool = True


class MinuteForecastRequest(BaseModel):
    """POST /research/minute-forecast — 종목 분봉 가격 예측.

    실행 규칙:
        - 08:00 KST 이전 호출 → 당일 장 운영 시간 (09:00~15:30) 390분봉 예측
        - 08:00 KST 이후 호출 → 다음 영업일 390분봉 예측
        - 이미 같은 (symbol, ts)가 존재하면 upsert

    요청 예시:
        {"symbol": "005930"}
        {"symbol": "005930", "save_feature_snapshot": true}
    """

    symbol: str = Field(
        ...,
        min_length=6,
        max_length=6,
        description="6자리 KRX 종목코드. 예: '005930' (삼성전자).",
    )
    save_feature_snapshot: bool = Field(
        False,
        description=(
            "True 이면 예측에 사용된 피처 값을 feature_snapshot(JSONB)에 저장. "
            "운영 환경에서는 용량 절약을 위해 False 당.  "
        ),
    )

    @field_validator("symbol")
    @classmethod
    def _zero_pad(cls, v: str) -> str:
        return v.strip().zfill(6)


class MinuteForecastResponse(BaseModel):
    """POST /research/minute-forecast 동기 실행 응답."""

    symbol: str
    target_date: str = Field(..., description="YYYY-MM-DD 형식 예측 대상 날짜.")
    n_rows: int = Field(..., description="DB에 upsert된 분봉 수 (정상: 390).")
    prev_close: float = Field(..., description="역변환 기준이 된 전일 종가.")
    model_version: str
    train_rows: int = Field(..., description="학습에 사용된 분봉 행 수.")


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
    "TickersUSRequest",
    "DartCorpCodesRequest",
    "DailyKISRequest",
    "DailyUSRequest",
    "DartDisclosuresRequest",
    "DartFinancialsRequest",
    "DartIndicatorsRequest",
    "ConsensusFnguideRequest",
    "ConsensusHankyungRequest",
    "DailyCronRequest",
    # scheduler
    "ScheduleEntry",
    "ScheduleListResponse",
    "SchedulePauseResponse",
    # health
    "HealthResponse",
]
