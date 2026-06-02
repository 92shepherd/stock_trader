"""KIS 1분봉 수집기.

수집 단위:
    symbol × 날짜. 날짜별로 09:00~15:30 전체 1분봉을 수집.

⚠️  KIS API 제약:
    inquire-time-itemchartprice (FHKST03010200) 는 **당일 데이터만** 지원.
    FID_PW_DATA_INCU_YN 은 과거 날짜 조회가 아닌 업종 시간대 포함 여부.
    따라서 target_date 는 반드시 오늘(장중/장마감 후) 또는
    FID_PW_DATA_INCU_YN="Y" 로 이전 날짜 경계를 건너는 방식 한정.

현실적 수집 범위:
    - 오늘 당일 분봉 수집 (장중 or 장마감 후 15:30~)
    - target_date 를 명시하면 FID_PW_DATA_INCU_YN="Y" 로 전일 페이징을 시도하나,
      KIS가 거부하는 경우 당일 데이터만 반환됨.
    - 과거 분봉 데이터가 필요하면 PyKRX 등 별도 소스 활용 권장.

페이징:
    cursor = "153000" 부터 역방향 30봉씩. 하루(390봉) = ~13 호출.

거래대금(value):
    acml_tr_pbmn(누적) 차분으로 분봉별 거래대금 계산.

Rate limit:
    real:  20 rps → 10 rps 목표 → 100ms/call
    paper:  5 rps → 2.5 rps 목표 → 400ms/call
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
from tqdm import tqdm

from src.collectors.daily_kis import _to_decimal, _to_int
from src.db.repositories import (
    get_active_tickers,
    get_completed_symbols_in_range,
    log_collection,
    upsert_minute_prices,
)
from src.kis.auth import get_kis_auth
from src.kis.minute import get_kis_minute
from src.kis.quotations import KISQuotationsError
from src.utils.logger import logger

COLLECTOR_NAME = "minute_kis"

# KIS API 분봉 조회 최대 기간 (거래일 기준 약 30일 → 42 캘린더일)
# KIS는 당일 ��이터만 지원 — target_date 는 오늘만 의미 있음.
# 안전 마진으로 최대 2 캘린더일(전일 포함)까지만 허용.
MAX_LOOKBACK_CALENDAR_DAYS = 2

_DEFAULT_DELAY_BY_MODE = {
    # 분봉 API는 일봉보다 호출 밀도가 높으므로 보수적으로 설정.
    # real: 20 rps 제한 → 10 rps 목표 (50% 헤드룸)
    # paper: 5 rps 제한 → 2.5 rps 목표
    "real": 0.1,
    "paper": 0.4,
}

_CIRCUIT_BREAKER_FAILURES = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trading_days(start: date, end: date) -> list[date]:
    """start~end 사이 평일(월~금) 목록. 공휴일은 KIS가 빈 응답으로 처리."""
    days: list[date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _bars_to_df(symbol: str, bars: list[dict[str, Any]]) -> pd.DataFrame:
    """KIS 분봉 raw bars → minute_prices DataFrame.

    Args:
        bars: 시간 오름차순 정렬된 raw bar list (KISMinute.fetch_day 반환값).

    Returns:
        symbol, ts(UTC tz-aware), open, high, low, close, volume, value 컬럼의 DataFrame.

    거래대금(value):
        acml_tr_pbmn(누적) 차분. 첫 봉은 acml_tr_pbmn 그대로.
        차분이 음수이면 NULL (날짜 경계 등 이상값).
    """
    rows = []
    prev_acml = 0

    for b in bars:
        date_str = b.get("stck_bsop_date", "")
        hour_str = b.get("stck_cntg_hour", "")
        if len(date_str) != 8 or len(hour_str) != 6:
            continue
        try:
            ts_naive = datetime(
                int(date_str[:4]), int(date_str[4:6]), int(date_str[6:]),
                int(hour_str[:2]), int(hour_str[2:4]), int(hour_str[4:]),
            )
        except ValueError:
            continue

        close = _to_decimal(b.get("stck_prpr"))
        if close is None or close <= 0:
            continue

        acml = _to_int(b.get("acml_tr_pbmn")) or 0
        bar_value = acml - prev_acml
        prev_acml = acml

        rows.append({
            "symbol": symbol,
            "ts": ts_naive,
            "open": _to_decimal(b.get("stck_oprc")),
            "high": _to_decimal(b.get("stck_hgpr")),
            "low": _to_decimal(b.get("stck_lwpr")),
            "close": close,
            "volume": _to_int(b.get("cntg_vol")),
            "value": bar_value if bar_value >= 0 else None,
        })

    if not rows:
        return pd.DataFrame(
            columns=["symbol", "ts", "open", "high", "low", "close", "volume", "value"]
        )

    df = pd.DataFrame(rows)
    # KST naive → UTC tz-aware (TimescaleDB TIMESTAMPTZ는 UTC 권장)
    df["ts"] = (
        pd.to_datetime(df["ts"])
        .dt.tz_localize("Asia/Seoul")
        .dt.tz_convert("UTC")
    )
    # None이 섞인 int 컬럼은 pandas가 float64로 승격 → bigint COPY 실패.
    # Int64(nullable integer)로 명시해 to_csv() 출력에서 소수점을 제거한다.
    for col in ("volume", "value"):
        if col in df.columns:
            df[col] = df[col].astype("Int64")
    return df


# ---------------------------------------------------------------------------
# Per-symbol-date collector
# ---------------------------------------------------------------------------


def collect_one_day(
    symbol: str,
    target_date: date,
    *,
    request_delay: float,
) -> int:
    """하루치 분봉 수집 → minute_prices upsert. 적재된 행 수 반환."""
    kis_minute = get_kis_minute()
    bars = kis_minute.fetch_day(symbol, target_date, request_delay=request_delay)
    if not bars:
        return 0
    df = _bars_to_df(symbol, bars)
    if df.empty:
        return 0
    return upsert_minute_prices(df)


# ---------------------------------------------------------------------------
# Backfill driver
# ---------------------------------------------------------------------------


def backfill_minutes(
    symbols: list[str] | None = None,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    skip_done: bool = True,
    request_delay: float | None = None,
    force_full_universe: bool = False,
) -> dict[str, Any]:
    """기간 내 1분봉 수집.

    Args:
        symbols: 종목코드 리스트. None이면 전체 활성 종목 (force_full_universe=True 필요).
        start_date: 수집 시작일. None 이면 30거래일 전으로 자동 설정.
        end_date:   수집 종료일. None 이면 오늘.
        skip_done:  collection_log 에 이미 성공한 (symbol, date) 쌍 스킵.
        request_delay: API 호출 간격(초). None=모드 기본값.
        force_full_universe: symbols=None 일 때 전체 종목 수집 허용 플래그.

    Returns:
        {total_rows, n_symbols, n_days, n_ok, n_skipped, n_failed}

    Raises:
        ValueError: symbols=None 이고 force_full_universe=False.
    """
    end_date = end_date or date.today()

    # KIS API 30거래일 제약
    min_start = end_date - timedelta(days=MAX_LOOKBACK_CALENDAR_DAYS)
    if start_date is None:
        start_date = min_start
    elif start_date < min_start:
        logger.warning(
            f"[minute_kis] start_date {start_date} exceeds 30-trading-day limit; "
            f"clamping to {min_start}"
        )
        start_date = min_start

    if symbols is None:
        if not force_full_universe:
            raise ValueError(
                "Collecting minute bars for the full universe is very slow "
                "(hours~days). Pass symbols=[...] or set force_full_universe=True "
                "to proceed."
            )
        from src.config import get_app_config
        cfg = get_app_config()
        tickers = get_active_tickers(markets=cfg.markets)
        symbols = [t.symbol for t in tickers]

    if not symbols:
        logger.warning("[minute_kis] No symbols to collect.")
        return {"total_rows": 0, "n_symbols": 0, "n_days": 0,
                "n_ok": 0, "n_skipped": 0, "n_failed": 0}

    if request_delay is None:
        mode = get_kis_auth().mode
        request_delay = _DEFAULT_DELAY_BY_MODE.get(mode, 0.25)
        logger.info(f"[minute_kis] mode={mode}, delay={request_delay}s/call")

    trading_days = _trading_days(start_date, end_date)
    if not trading_days:
        logger.info("[minute_kis] No trading days in range.")
        return {"total_rows": 0, "n_symbols": len(symbols), "n_days": 0,
                "n_ok": 0, "n_skipped": 0, "n_failed": 0}

    # skip_done: collection_log 에서 완료된 (symbol, date) 조회
    done_set: set[tuple[str, date]] = set()
    if skip_done:
        for d in trading_days:
            for s in get_completed_symbols_in_range(COLLECTOR_NAME, d, d):
                done_set.add((s, d))
        logger.info(f"[minute_kis] {len(done_set)} (symbol, date) pairs already done.")

    logger.info(
        f"[minute_kis] {len(symbols)} symbols × {len(trading_days)} days "
        f"[{start_date} → {end_date}]"
    )

    total_rows = 0
    n_ok = n_skipped = n_failed = 0
    consecutive_failures = 0

    for symbol in tqdm(symbols, desc="minute_kis"):
        for target_date in trading_days:
            if (symbol, target_date) in done_set:
                n_skipped += 1
                continue

            t0 = time.time()
            try:
                rows = collect_one_day(
                    symbol, target_date, request_delay=request_delay
                )
                dur = int((time.time() - t0) * 1000)

                if rows == 0:
                    log_collection(
                        COLLECTOR_NAME, target_date, "skipped",
                        symbol=symbol, duration_ms=dur,
                    )
                    n_skipped += 1
                else:
                    log_collection(
                        COLLECTOR_NAME, target_date, "success",
                        symbol=symbol, rows_inserted=rows, duration_ms=dur,
                    )
                    total_rows += rows
                    n_ok += 1

                consecutive_failures = 0

            except KISQuotationsError as e:
                dur = int((time.time() - t0) * 1000)
                logger.warning(f"[minute_kis] {symbol} {target_date} biz error: {e}")
                log_collection(
                    COLLECTOR_NAME, target_date, "failed",
                    symbol=symbol, error_message=str(e)[:500], duration_ms=dur,
                )
                n_failed += 1
                consecutive_failures += 1

            except Exception as e:
                dur = int((time.time() - t0) * 1000)
                logger.error(f"[minute_kis] {symbol} {target_date} failed: {e}")
                log_collection(
                    COLLECTOR_NAME, target_date, "failed",
                    symbol=symbol, error_message=str(e)[:500], duration_ms=dur,
                )
                n_failed += 1
                consecutive_failures += 1

            if consecutive_failures >= _CIRCUIT_BREAKER_FAILURES:
                logger.error(
                    f"[minute_kis] Circuit breaker: {consecutive_failures} "
                    "consecutive failures. Aborting."
                )
                return {
                    "total_rows": total_rows,
                    "n_symbols": len(symbols),
                    "n_days": len(trading_days),
                    "n_ok": n_ok,
                    "n_skipped": n_skipped,
                    "n_failed": n_failed,
                    "aborted": True,
                }

    logger.success(
        f"[minute_kis] Done. rows={total_rows:,} ok={n_ok} "
        f"skipped={n_skipped} failed={n_failed}"
    )
    return {
        "total_rows": total_rows,
        "n_symbols": len(symbols),
        "n_days": len(trading_days),
        "n_ok": n_ok,
        "n_skipped": n_skipped,
        "n_failed": n_failed,
    }


# ---------------------------------------------------------------------------
# 실시간 수집 — 매분 스케줄러 전용
# ---------------------------------------------------------------------------

def collect_latest_bars(
    symbols: list[str],
    *,
    request_delay: float | None = None,
) -> dict[str, Any]:
    """현재 시각 기준 최근 봉을 종목별로 1회 호출해 upsert.

    매분 스케줄러 잡에서 호출되는 함수.
    각 종목마다 KIS API 1회 호출 → 최근 30봉(당일) 수신 → upsert.

    Args:
        symbols:       수집 대상 종목코드 리스트.
        request_delay: API 호출 간격(초). None=모드 기본값.

    Returns:
        {total_rows, n_symbols, n_ok, n_failed, skipped_market_closed}
    """
    from src.kis.market_status import is_market_open
    if not is_market_open():
        return {
            "total_rows": 0,
            "n_symbols": len(symbols),
            "n_ok": 0,
            "n_failed": 0,
            "skipped_market_closed": True,
        }

    if request_delay is None:
        mode = get_kis_auth().mode
        request_delay = _DEFAULT_DELAY_BY_MODE.get(mode, 0.4)

    kis_minute = get_kis_minute()
    total_rows = 0
    n_ok = 0
    n_failed = 0

    for symbol in symbols:
        try:
            bars = kis_minute.fetch_latest(symbol)
            time.sleep(request_delay)
            if not bars:
                continue
            df = _bars_to_df(symbol, bars)
            if df.empty:
                continue
            rows = upsert_minute_prices(df)
            total_rows += rows
            n_ok += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[minute_realtime] {symbol} failed: {e}")
            n_failed += 1

    logger.debug(
        f"[minute_realtime] rows={total_rows} ok={n_ok} failed={n_failed} "
        f"symbols={len(symbols)}"
    )
    return {
        "total_rows": total_rows,
        "n_symbols": len(symbols),
        "n_ok": n_ok,
        "n_failed": n_failed,
        "skipped_market_closed": False,
    }


def collect_today_bars(
    symbols: list[str],
    *,
    require_market_open: bool = True,
    request_delay: float | None = None,
) -> dict[str, Any]:
    """장중 주기 수집 — 종목별 '당일 전체' 분봉을 페이징 수집해 upsert.

    collect_latest_bars 는 호출당 최근 ~30봉만 가져와 주기가 길면(예:
    1시간) 봉 공백이 생긴다. 이 함수는 각 종목에 대해 collect_one_day(오늘)
    로 당일 09:00~현재까지 전체 분봉을 페이징 수집한다. upsert 가 멱등
    (ON CONFLICT)이라 매 주기 당일 전체를 다시 긁어도 중복 없이 채워진다.

    비용: 종목당 ~13콜(당일 페이징)로 collect_latest_bars(1콜/종목)보다
    호출량이 많다. 관심종목(수십 개) 단위 사용 권장. 종목 간 간격은
    fetch_day 내부의 페이지별 sleep 으로 자연히 확보된다.

    장외(09:00~15:30 KST 이외)에는 즉시 스킵(skipped_market_closed=True).
    require_market_open=False 면 이 게이트를 무시한다(마감 후 1회 최종
    수집용 — 당일 전체를 다시 긁어 마지막 ~30분 tail 까지 채운다).

    Returns:
        {total_rows, n_symbols, n_ok, n_failed, skipped_market_closed}
        (collect_latest_bars 와 동일 스키마 — 스케줄러 로깅 호환)
    """
    from zoneinfo import ZoneInfo

    from src.kis.market_status import is_market_open

    if require_market_open and not is_market_open():
        return {
            "total_rows": 0,
            "n_symbols": len(symbols),
            "n_ok": 0,
            "n_failed": 0,
            "skipped_market_closed": True,
        }

    if request_delay is None:
        mode = get_kis_auth().mode
        request_delay = _DEFAULT_DELAY_BY_MODE.get(mode, 0.4)

    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    total_rows = 0
    n_ok = 0
    n_failed = 0

    for symbol in symbols:
        try:
            rows = collect_one_day(symbol, today, request_delay=request_delay)
            total_rows += rows
            n_ok += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[minute_today] {symbol} failed: {e}")
            n_failed += 1

    logger.debug(
        f"[minute_today] rows={total_rows} ok={n_ok} failed={n_failed} "
        f"symbols={len(symbols)}"
    )
    return {
        "total_rows": total_rows,
        "n_symbols": len(symbols),
        "n_ok": n_ok,
        "n_failed": n_failed,
        "skipped_market_closed": False,
    }


def collect_recent_window(
    symbols: list[str],
    *,
    lookback_minutes: int = 120,
    request_delay: float | None = None,
) -> dict[str, Any]:
    """장중 주기 수집 — 종목별 '최근 lookback_minutes 분'(기본 120=2시간) 롤링 수집.

    주기보다 긴 lookback 으로 매 호출을 직전 구간과 겹치게 하면, 타이밍
    지터가 있어도 봉 공백이 생기지 않는다(예: 60분 주기 + 120분 lookback
    → 매 호출 1시간 중복). upsert 멱등(ON CONFLICT)이라 중복은 흡수된다.

    비용: 종목당 ceil(lookback/30) 콜(120분→~4콜)로 collect_today_bars
    (당일 전체, ~13콜)보다 가볍다 → paper 모드에서도 부담이 적다.

    종목 간 간격은 fetch_recent 내부 페이지별 sleep 으로 확보.
    장외(09:00~15:30 KST 이외)에는 즉시 스킵.

    Returns:
        {total_rows, n_symbols, n_ok, n_failed, skipped_market_closed}
        (collect_latest_bars/collect_today_bars 와 동일 스키마)
    """
    from src.kis.market_status import is_market_open

    if not is_market_open():
        return {
            "total_rows": 0,
            "n_symbols": len(symbols),
            "n_ok": 0,
            "n_failed": 0,
            "skipped_market_closed": True,
        }

    if request_delay is None:
        mode = get_kis_auth().mode
        request_delay = _DEFAULT_DELAY_BY_MODE.get(mode, 0.4)

    kis_minute = get_kis_minute()
    total_rows = 0
    n_ok = 0
    n_failed = 0

    for symbol in symbols:
        try:
            bars = kis_minute.fetch_recent(
                symbol,
                lookback_minutes=lookback_minutes,
                request_delay=request_delay,
            )
            if bars:
                df = _bars_to_df(symbol, bars)
                if not df.empty:
                    total_rows += upsert_minute_prices(df)
            n_ok += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[minute_recent] {symbol} failed: {e}")
            n_failed += 1

    logger.debug(
        f"[minute_recent] rows={total_rows} ok={n_ok} failed={n_failed} "
        f"symbols={len(symbols)} lookback={lookback_minutes}m"
    )
    return {
        "total_rows": total_rows,
        "n_symbols": len(symbols),
        "n_ok": n_ok,
        "n_failed": n_failed,
        "skipped_market_closed": False,
    }
