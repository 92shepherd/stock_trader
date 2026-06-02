"""Read-only data query endpoints — 분봉 조회 / 분봉 예측 조회.

수집(/collect)·평가(/research) 트리거 라우터가 전부 POST 비동기 잡인 것과
달리, 이 라우터는 **이미 적재된 데이터를 읽어 반환**하는 동기 GET 엔드포인트만
제공한다. 락(lock)이나 잡 레지스트리를 거치지 않고 단일 SELECT 만 수행한다.

Endpoints:
  - GET /query/minute/prices       — 실측 1분봉 OHLCV (symbol + date)
  - GET /query/minute/predictions  — 분봉 예측값 (symbol + date)

조회 단위:
  날짜(date) 하나 + 종목(symbol) 하나. 해당 KST 달력일의
  [00:00:00, 23:59:59] 범위 분봉을 ts 오름차순으로 반환한다. 한국 장
  운영시간(09:00~15:30)만 데이터가 존재하므로 실질 반환 범위도 동일하다.

인증:
  다른 비-health 라우터와 동일하게 X-API-Key(STOCK_TRADER_API_KEY) 필요.
"""
from __future__ import annotations

import math
from datetime import date as date_type, datetime
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import APIRouter, Depends, Query

from src.api.auth import require_api_key
from src.api.schemas import (
    MinuteBar,
    MinutePrediction,
    MinutePredictionsQueryResponse,
    MinutePricesQueryResponse,
)
from src.db.repositories import (
    query_minute_price_predictions,
    query_minute_prices,
)

# 한국 시간대 — 조회 날짜를 KST 달력일로 해석한다.
KST = ZoneInfo("Asia/Seoul")

router = APIRouter(
    prefix="/query",
    tags=["query"],
    dependencies=[Depends(require_api_key)],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _day_bounds_kst(d: date_type) -> tuple[datetime, datetime]:
    """KST 달력일 d 의 [00:00:00, 23:59:59] 양끝 포함 경계를 반환.

    repository 의 ts-range 조회 시그니처(ts >= start AND ts <= end)에
    맞춰 양끝 포함 형태로 만든다. 분봉/예측 ts 는 분 단위(:00초)이므로
    23:59:59 상한으로 당일 마지막 분봉(15:30)까지 모두 포함되고 익일
    00:00 은 포함되지 않는다.
    """
    start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=KST)
    end = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=KST)
    return start, end


def _to_float(v: object) -> float | None:
    """NUMERIC/Decimal/NaN 안전 변환. 비유한값은 None 으로."""
    if v is None:
        return None
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _to_int(v: object) -> int | None:
    """BIGINT 안전 변환 (pandas NaN → None)."""
    f = _to_float(v)
    return int(f) if f is not None else None


def _norm_symbol(symbol: str) -> str:
    """입력 종목코드를 6자리 zero-pad 정규화 (예: '5930' → '005930')."""
    return symbol.strip().zfill(6)


def _ts_iso_kst(df: pd.DataFrame) -> pd.DataFrame:
    """ts 컬럼을 KST tz-aware 로 정규화한 사본을 반환.

    pd.read_sql 은 TIMESTAMPTZ 를 UTC(또는 naive) 로 돌려줄 수 있으므로
    utc=True 로 일단 맞춘 뒤 KST 로 변환한다.
    """
    out = df.copy()
    out["ts"] = pd.to_datetime(out["ts"], utc=True).dt.tz_convert(KST)
    return out


# ---------------------------------------------------------------------------
# 실측 분봉 조회
# ---------------------------------------------------------------------------


@router.get(
    "/minute/prices",
    response_model=MinutePricesQueryResponse,
    summary="실측 1분봉 조회 (종목 + 날짜)",
)
async def get_minute_prices(
    symbol: str = Query(
        ...,
        min_length=1,
        max_length=10,
        description="6자리 KRX 종목코드. 예: 005930 (삼성전자). 짧게 입력 시 zero-pad.",
    ),
    date: date_type = Query(
        ...,
        description="조회 날짜 (YYYY-MM-DD, KST 기준). 해당 일자의 모든 분봉 반환.",
    ),
) -> MinutePricesQueryResponse:
    """`minute_prices` 에서 특정 종목/날짜의 실측 1분봉 OHLCV 를 조회.

    - ts 오름차순 정렬.
    - 데이터가 없으면 count=0, bars=[] (404 아님).
    - 분봉 백필 범위는 KIS API 특성상 최근 30거래일 위주임에 유의.
    """
    sym = _norm_symbol(symbol)
    start, end = _day_bounds_kst(date)
    df = query_minute_prices(sym, start, end)

    bars: list[MinuteBar] = []
    if not df.empty:
        df = _ts_iso_kst(df)
        for row in df.itertuples(index=False):
            bars.append(
                MinuteBar(
                    ts=row.ts.isoformat(),
                    open=_to_float(row.open),
                    high=_to_float(row.high),
                    low=_to_float(row.low),
                    close=_to_float(row.close),
                    volume=_to_int(row.volume),
                    value=_to_int(row.value),
                )
            )

    return MinutePricesQueryResponse(
        symbol=sym,
        date=date.isoformat(),
        count=len(bars),
        bars=bars,
    )


# ---------------------------------------------------------------------------
# 분봉 예측값 조회
# ---------------------------------------------------------------------------


@router.get(
    "/minute/predictions",
    response_model=MinutePredictionsQueryResponse,
    summary="분봉 예측값 조회 (종목 + 날짜)",
)
async def get_minute_predictions(
    symbol: str = Query(
        ...,
        min_length=1,
        max_length=10,
        description="6자리 KRX 종목코드. 예: 005930 (삼성전자). 짧게 입력 시 zero-pad.",
    ),
    date: date_type = Query(
        ...,
        description="조회 날짜 (YYYY-MM-DD, KST 기준). 해당 일자 예측 포인트 반환.",
    ),
) -> MinutePredictionsQueryResponse:
    """`minute_price_predictions` 에서 특정 종목/날짜의 분봉 예측값을 조회.

    - LightGBM per-symbol 모델이 생성한 1분 단위 예측(정상 시 390포인트).
    - ts 오름차순 정렬.
    - 데이터가 없으면 count=0, predictions=[] (404 아님).
      예측은 `POST /research/minute-forecast` 로 미리 생성되어 있어야 한다.
    """
    sym = _norm_symbol(symbol)
    start, end = _day_bounds_kst(date)
    df = query_minute_price_predictions(sym, start, end)

    preds: list[MinutePrediction] = []
    model_version: str | None = None
    if not df.empty:
        df = _ts_iso_kst(df)
        for row in df.itertuples(index=False):
            model_version = row.model_version
            preds.append(
                MinutePrediction(
                    ts=row.ts.isoformat(),
                    predicted_return=_to_float(row.predicted_return),
                    predicted_close=_to_float(row.predicted_close),
                    prev_close=_to_float(row.prev_close),
                    model_version=row.model_version,
                )
            )

    return MinutePredictionsQueryResponse(
        symbol=sym,
        date=date.isoformat(),
        count=len(preds),
        model_version=model_version,
        predictions=preds,
    )
