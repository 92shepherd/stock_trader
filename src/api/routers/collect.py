"""Collector trigger endpoints.

Endpoint conventions:
  - Short collectors (tickers, dart_corp_codes) are POST and run
    SYNCHRONOUSLY \u2014 response carries the result.
  - Long collectors (daily_*, dart_disclosures, dart_financials,
    dart_indicators) are POST and run ASYNCHRONOUSLY \u2014 response
    carries a job_id. Track via GET /jobs/{id}.
  - The composite `POST /collect/daily-cron` runs the same KIS+DART
    pair that the scheduled cron runs. Useful for manual re-runs or
    catch-up after an outage.

Locking model (see src/api/locks.py for details):
  - Every concrete collector has its own asyncio.Lock + PG advisory
    lock. If another caller is already running that collector, sync
    routes return 409; async routes accept the job but mark it
    REJECTED in the registry so /jobs/{id} reports the busy reason.
  - The daily-cron path takes `DAILY_CRON`, a SEPARATE composite lock.
    That lock does NOT block, e.g., a manual `daily_fdr` run from
    being submitted in parallel \u2014 only same-collector collisions are
    prevented. We chose that on purpose: blocking unrelated collectors
    during a 30-min KIS backfill would surprise operators.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from src.api.auth import require_api_key
from src.api.locks import CollectorBusy
from src.api.runners import (
    run_dart_corp_codes,
    run_tickers_kr,
    run_tickers_us,
    submit_consensus_fnguide,
    submit_consensus_hankyung,
    submit_daily_cron,
    submit_daily_fdr,
    submit_daily_kis,
    submit_daily_us,
    submit_dart_disclosures,
    submit_dart_financials,
    submit_dart_indicators,
    submit_minute_kis,
)
from src.api.schemas import (
    ConsensusFnguideRequest,
    ConsensusHankyungRequest,
    DailyCronRequest,
    DailyFDRRequest,
    DailyKISRequest,
    DailyUSRequest,
    DartCorpCodesRequest,
    DartDisclosuresRequest,
    DartFinancialsRequest,
    DartIndicatorsRequest,
    JobAcceptedResponse,
    MinuteKISRequest,
    SyncRunResponse,
    TickersKRRequest,
    TickersUSRequest,
)

router = APIRouter(
    prefix="/collect",
    tags=["collect"],
    dependencies=[Depends(require_api_key)],
)


def _job_accepted(record) -> JobAcceptedResponse:
    """Convert a JobRecord to the 202 envelope."""
    return JobAcceptedResponse(
        job_id=record.id,
        collector=record.collector.value,
        status="pending",
        submitted_at=record.submitted_at.isoformat(),
    )


def _busy_409(exc: CollectorBusy) -> HTTPException:
    """Standard 409 mapping for sync triggers when the lock is held."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "collector": exc.collector.value,
            "reason": exc.reason,
            "advice": (
                "Wait for the running task to finish or check GET /jobs."
            ),
        },
    )


# ===========================================================================
# Master-data collectors \u2014 SYNCHRONOUS
# ===========================================================================


@router.post(
    "/tickers/kr",
    response_model=SyncRunResponse,
    summary="Refresh Korean ticker master (FDR)",
)
async def trigger_tickers_kr(req: TickersKRRequest) -> SyncRunResponse:
    try:
        result = await run_tickers_kr(desc=req.desc)
    except CollectorBusy as e:
        raise _busy_409(e) from e
    return SyncRunResponse(**result)


@router.post(
    "/tickers/us",
    response_model=SyncRunResponse,
    summary="Refresh US ticker master (NASDAQ Trader)",
)
async def trigger_tickers_us(req: TickersUSRequest) -> SyncRunResponse:
    # req kept for symmetry / future extension (e.g. exchange filter).
    _ = req
    try:
        result = await run_tickers_us()
    except CollectorBusy as e:
        raise _busy_409(e) from e
    return SyncRunResponse(**result)


@router.post(
    "/dart/corp-codes",
    response_model=SyncRunResponse,
    summary="Refresh DART corp_codes (skipped if fresh)",
)
async def trigger_dart_corp_codes(req: DartCorpCodesRequest) -> SyncRunResponse:
    try:
        result = await run_dart_corp_codes(
            force=req.force,
            stale_after_days=req.stale_after_days,
        )
    except CollectorBusy as e:
        raise _busy_409(e) from e
    return SyncRunResponse(**result)


# ===========================================================================
# Daily-price collectors \u2014 ASYNCHRONOUS (job_id)
# ===========================================================================


@router.post(
    "/daily/fdr",
    response_model=JobAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Korean daily prices via FinanceDataReader (long-running)",
)
async def trigger_daily_fdr(req: DailyFDRRequest) -> JobAcceptedResponse:
    record = await submit_daily_fdr(
        symbols=req.symbols,
        start_date=req.start_date,
        end_date=req.end_date,
        days=req.days,
        markets=req.markets,
        skip_done=req.skip_done,
    )
    return _job_accepted(record)


@router.post(
    "/daily/kis",
    response_model=JobAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Korean daily prices via KIS API (long-running)",
)
async def trigger_daily_kis(req: DailyKISRequest) -> JobAcceptedResponse:
    record = await submit_daily_kis(
        symbols=req.symbols,
        start_date=req.start_date,
        end_date=req.end_date,
        days=req.days,
        skip_done=req.skip_done,
        fetch_snapshot=req.fetch_snapshot,
        request_delay=req.request_delay,
    )
    return _job_accepted(record)


@router.post(
    "/daily/us",
    response_model=JobAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="US daily prices via yfinance",
)
async def trigger_daily_us(req: DailyUSRequest) -> JobAcceptedResponse:
    record = await submit_daily_us(
        symbols=req.symbols,
        start_date=req.start_date,
        end_date=req.end_date,
        days=req.days,
        exchanges=req.exchanges,
        security_types=req.security_types,
        skip_done=req.skip_done,
    )
    return _job_accepted(record)


@router.post(
    "/consensus/hankyung",
    response_model=JobAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="한경 컨센서스 리포트 수집 (날짜 지정 또는 1년 백필)",
)
async def trigger_consensus_hankyung(
    req: ConsensusHankyungRequest,
) -> JobAcceptedResponse:
    """한경 컨센서스(markets.hankyung.com) 애널리스트 리포트 수집 트리거.

    HANKYUNG_ENABLED=true 가 환경변수에 없으면 collector 가 RuntimeError 로
    거부하고 job 은 'failed' 로 마킹됨.

    수집 모드:
        - backfill_year=true → 1년치 백필
        - from_date + to_date → 날짜 범위
        - target_date → 하루
        - 파라미터 없음 → 오늘 하루
    """
    record = await submit_consensus_hankyung(
        target_date=req.target_date,
        from_date=req.from_date,
        to_date=req.to_date,
        backfill_year=req.backfill_year,
        skip_done=req.skip_done,
        request_delay=req.request_delay,
    )
    return _job_accepted(record)


# ===========================================================================
# DART data collectors \u2014 ASYNCHRONOUS
# ===========================================================================


@router.post(
    "/dart/disclosures",
    response_model=JobAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="DART disclosures for a date range",
)
async def trigger_dart_disclosures(
    req: DartDisclosuresRequest,
) -> JobAcceptedResponse:
    record = await submit_dart_disclosures(
        start_date=req.start_date,
        end_date=req.end_date,
        days=req.days,
        kinds=req.kinds,
        listed_only=req.listed_only,
        skip_done=req.skip_done,
    )
    return _job_accepted(record)


@router.post(
    "/dart/financials",
    response_model=JobAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="DART major-account financials (very long-running)",
)
async def trigger_dart_financials(
    req: DartFinancialsRequest,
) -> JobAcceptedResponse:
    record = await submit_dart_financials(
        start_year=req.start_year,
        end_year=req.end_year,
        reprt_codes=req.reprt_codes,
        fs_divs=req.fs_divs,
        corp_codes=req.corp_codes,
        skip_done=req.skip_done,
        max_calls=req.max_calls,
    )
    return _job_accepted(record)


@router.post(
    "/dart/indicators",
    response_model=JobAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="DART financial indicators (very long-running)",
)
async def trigger_dart_indicators(
    req: DartIndicatorsRequest,
) -> JobAcceptedResponse:
    record = await submit_dart_indicators(
        start_year=req.start_year,
        end_year=req.end_year,
        reprt_codes=req.reprt_codes,
        fs_divs=req.fs_divs,
        idx_cl_codes=req.idx_cl_codes,
        corp_codes=req.corp_codes,
        skip_done=req.skip_done,
        max_calls=req.max_calls,
    )
    return _job_accepted(record)


# ===========================================================================
# Consensus collectors
# ===========================================================================


@router.post(
    "/consensus/fnguide",
    response_model=JobAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="FnGuide consensus snapshot (개인용 비공개 한정)",
)
async def trigger_consensus_fnguide(
    req: ConsensusFnguideRequest,
) -> JobAcceptedResponse:
    """FnGuide 컨센서스 일 스냅샷 수집 트리거.

    명시적 주의:
        FnGuide 약관은 데이터베이스화 제한 조항이 있어 본 엔드포인트는
        **개인 트레이딩 시스템의 비공개 사용** 한정. .env 에
        FNGUIDE_CONSENT_ACK=1 이 없으면 collector 가 RuntimeError 로
        거부하고 job 은 'failed' 로 마킹됨.
    """
    record = await submit_consensus_fnguide(
        symbols=req.symbols,
        as_of_date=req.as_of_date,
        markets=req.markets,
        skip_done=req.skip_done,
        request_delay=req.request_delay,
    )
    return _job_accepted(record)


# ===========================================================================
# Composite daily cron \u2014 manual trigger of the 03:00 KST sequence
# ===========================================================================


@router.post(
    "/daily-cron",
    response_model=JobAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run the full daily KIS+DART sequence (same as scheduled cron)",
)
async def trigger_daily_cron(req: DailyCronRequest) -> JobAcceptedResponse:
    record = await submit_daily_cron(
        end_date=req.end_date,
        days=req.days,
        only=req.only,
        fetch_snapshot=req.fetch_snapshot,
        skip_done=req.skip_done,
    )
    return _job_accepted(record)


# ===========================================================================
# Minute prices — KIS 1분봉
# ===========================================================================


@router.post(
    "/minute/kis",
    response_model=JobAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="KIS 1분봉 수집 (최근 30거래일)",
    description=(
        "KIS inquire-time-itemchartprice 를 이용해 1분봉을 수집하여 "
        "minute_prices 테이블에 적재합니다.\n\n"
        "**기간 제약**: KIS API 는 최근 30거래일까지만 제공합니다. "
        "start_date 를 생략하면 자동으로 30거래일 전으로 설정됩니다.\n\n"
        "**성능 주의**: 전체 활성 종목 수집은 수십 시간 소요됩니다. "
        "symbols 파라미터로 대상을 좁히거나 force_full_universe=true 를 "
        "명시적으로 설정하세요."
    ),
)
async def trigger_minute_kis(req: MinuteKISRequest) -> JobAcceptedResponse:
    try:
        record = await submit_minute_kis(
            symbols=req.symbols,
            start_date=req.start_date,
            end_date=req.end_date,
            skip_done=req.skip_done,
            request_delay=req.request_delay,
            force_full_universe=req.force_full_universe,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return _job_accepted(record)
