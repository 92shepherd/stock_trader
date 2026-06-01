"""Research trigger endpoints — factor evaluation.

Endpoint conventions:
  - POST /research/factor/evaluate      — 단일 팩터 평가 (비동기 잡)
  - POST /research/factor/evaluate/all  — 전체(또는 지정) 팩터 일괄 평가 (비동기 잡)
  - GET  /research/factor/registry      — 등록된 팩터 이름 목록 조회

Locking:
  두 엔드포인트 모두 FACTOR_EVAL 락을 공유. 동시에 두 평가 잡이 실행되면
  409 를 반환한다.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from src.api.auth import require_api_key
from src.api.locks import CollectorBusy
from src.api.runners import (
    _build_factor_registry,
    submit_factor_eval,
    submit_factor_eval_all,
    submit_minute_forecast,
)
from src.api.schemas import (
    FactorEvalAllRequest,
    FactorEvalRequest,
    JobAcceptedResponse,
    MinuteForecastRequest,
    MinuteForecastResponse,
)

router = APIRouter(
    prefix="/research",
    tags=["research"],
    dependencies=[Depends(require_api_key)],
)


def _job_accepted(record) -> JobAcceptedResponse:
    return JobAcceptedResponse(
        job_id=record.id,
        collector=record.collector.value,
        status="pending",
        submitted_at=record.submitted_at.isoformat(),
    )


def _busy_409(exc: CollectorBusy) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "collector": exc.collector.value,
            "reason": exc.reason,
            "advice": "Wait for the running factor evaluation to finish or check GET /jobs.",
        },
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@router.get(
    "/factor/registry",
    summary="등록된 팩터 이름 목록",
    response_model=dict,
)
async def get_factor_registry() -> dict:
    """평가 가능한 팩터 이름 전체 목록을 그룹별로 반환."""
    from src.research.factors_baseline import BASELINE_FACTORS
    from src.research.factors_pead import PEAD_FACTORS
    from src.research.factors_quality import QUALITY_FACTORS
    from src.research.factors_rating import RATING_FACTORS
    from src.research.factors_revision import REVISION_FACTORS

    return {
        "baseline": list(BASELINE_FACTORS.keys()),
        "pead": list(PEAD_FACTORS.keys()),
        "quality": list(QUALITY_FACTORS.keys()),
        "rating": list(RATING_FACTORS.keys()),
        "revision": list(REVISION_FACTORS.keys()),
        "total": (
            len(BASELINE_FACTORS)
            + len(PEAD_FACTORS)
            + len(QUALITY_FACTORS)
            + len(RATING_FACTORS)
            + len(REVISION_FACTORS)
        ),
    }


# ---------------------------------------------------------------------------
# Factor evaluation
# ---------------------------------------------------------------------------


@router.post(
    "/factor/evaluate",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAcceptedResponse,
    summary="단일 팩터 평가 (비동기)",
)
async def trigger_factor_eval(req: FactorEvalRequest) -> JobAcceptedResponse:
    """단일 팩터를 평가하고 eval_runs / eval_metrics 에 결과를 저장.

    - 진행 상황은 `GET /jobs/{job_id}` 로 확인.
    - factor_name 은 `GET /research/factor/registry` 에서 확인 가능.
    - persist_signals=true 는 factor_signals 에 신호를 저장하므로 디스크 사용량에 주의.
    """
    registry = _build_factor_registry()
    if req.factor_name not in registry:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "factor_name": req.factor_name,
                "error": "Unknown factor. Check GET /research/factor/registry.",
            },
        )
    try:
        record = await submit_factor_eval(
            factor_name=req.factor_name,
            universe=req.universe,
            start_date=req.start_date,
            end_date=req.end_date,
            horizon_days=req.horizon_days,
            persist_signals=req.persist_signals,
            persist_run=req.persist_run,
        )
    except CollectorBusy as e:
        raise _busy_409(e) from e
    return _job_accepted(record)


# ---------------------------------------------------------------------------
# Minute-bar price forecast
# ---------------------------------------------------------------------------


@router.post(
    "/minute-forecast",
    response_model=MinuteForecastResponse,
    summary="분봉 가격 예측 (LightGBM per-symbol)",
)
async def trigger_minute_forecast(
    req: MinuteForecastRequest,
) -> MinuteForecastResponse:
    """LightGBM 모델로 특정 종목의 당일/익일 분봉 예측 생성.

    실행 규칙:
        - 08:00 KST **이전** 호출 → **당일** 09:00~15:30 (390분봉) 예측
        - 08:00 KST **이후** 호출 → **다음 영업일** 09:00~15:30 예측
        - 이미 같은 (symbol, ts)가 존재하면 upsert

    입력 데이터:
        - 직전 22거래일 분봉 (minute_prices 테이블)
        - 일봉 모멘텀/편더멘털 (daily_prices)
        - 공시 유무 (dart_disclosures)
        - EPS/목표주가 콘센서스 (consensus_estimates)

    예측 결과는 `minute_price_predictions` 테이블에 저장됩니다.
    """
    try:
        record = await submit_minute_forecast(
            symbol=req.symbol,
            save_feature_snapshot=req.save_feature_snapshot,
        )
    except CollectorBusy as e:
        raise _busy_409(e) from e

    # 작업이 완료될 때까지 대기 (분봉 예측은 대개 1~5초 소요)
    # _run_async_locked 담당 작업이 백그라운드에서 실행되므로,
    # 완료를 기다려 결과를 직접 반환.
    import asyncio as _asyncio
    from src.api.jobs import get_registry

    registry = get_registry()
    for _ in range(120):  # 최대 60초 대기
        job = await registry.get(record.id)
        if job is None:
            break
        if job.status in ("success", "failed", "rejected"):
            break
        await _asyncio.sleep(0.5)

    job = await registry.get(record.id)
    if job is None or job.status != "success" or job.result is None:
        error_msg = (job.error if job else None) or "unknown error"
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "symbol": req.symbol,
                "error": error_msg,
                "job_id": record.id,
            },
        )

    r = job.result
    return MinuteForecastResponse(
        symbol=r["symbol"],
        target_date=r["target_date"],
        n_rows=r["n_rows"],
        prev_close=r["prev_close"],
        model_version=r["model_version"],
        train_rows=r["train_rows"],
    )


@router.post(
    "/factor/evaluate/all",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobAcceptedResponse,
    summary="전체(또는 지정) 팩터 일괄 평가 (비동기)",
)
async def trigger_factor_eval_all(req: FactorEvalAllRequest) -> JobAcceptedResponse:
    """등록된 모든 팩터(또는 factors 리스트에 지정한 팩터)를 순서대로 평가.

    개별 팩터 에러가 발생해도 나머지 팩터 평가는 계속 진행됨.
    결과는 `GET /jobs/{job_id}` 의 `result.metrics` 에서 팩터별로 확인 가능.
    """
    try:
        record = await submit_factor_eval_all(
            universe=req.universe,
            start_date=req.start_date,
            end_date=req.end_date,
            horizon_days=req.horizon_days,
            factors=req.factors,
            persist_signals=req.persist_signals,
            persist_run=req.persist_run,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": str(e)},
        ) from e
    except CollectorBusy as e:
        raise _busy_409(e) from e
    return _job_accepted(record)
