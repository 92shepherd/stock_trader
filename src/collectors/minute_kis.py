"""KIS 1분봉 수집기.

수집 단위:
    (symbol, date) 쌍. 날짜별로 09:00~15:30 전체 1분봉을 수집.

거래 가능 기간:
    최근 30거래일 (KIS API 제약). start_date 가 이보다 오래되면 자동으로 클램프.

페이징:
    KIS inquire-time-itemchartprice 는 1회에 최대 30봉.
    하루(390봉) 수집에 ~13번 API 호출 필요.
    target_date 가 N거래일 전이면 중간 날짜들도 페이징으로 통과해야 하므로
    오래된 날짜일수록 호출 횟수가 증가함 (N=1: ~26회, N=20: ~260회/종목).

성능 가이드:
    - 전체 활성 종목(~2,600개)에 대해 30거래일 수집은 수십 시간 소요.
    - 실사용 권장: symbols 파라미터로 대상 종목을 명시하거나,
      force_full_universe=True 플래그를 명시적으로 설정.

거래대금(value) 산출:
    KIS API 는 누적거래대금(acml_tr_pbmn)만 제공.
    같은 날짜 내 연속 봉의 차분으로 분봉 거래대금을 계산.
    첫 봉(09:00)은 acml_tr_pbmn 값을 그대로 사용.

Rate limit:
    real:  20 rps → 15 rps target → 67ms/call
    paper:  5 rps →  4 rps target → 250ms/call
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
MAX_LOOKBACK_CALENDAR_DAYS = 42

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
