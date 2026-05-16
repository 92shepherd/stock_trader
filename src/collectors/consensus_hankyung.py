"""한경 컨센서스 수집기 (markets.hankyung.com).

Source endpoint:
    GET https://markets.hankyung.com/api/consensus/search/report
    Authorization: Bearer <static token embedded in Nuxt JS bundle>

    파라미터:
        fromDate    YYYY-MM-DD
        toDate      YYYY-MM-DD
        reportType  CO (기업 리포트만 — 종목코드 있는 것)
        page        1-based
        per_page    최대 100 권장

    응답:
        {current_page, per_page, total, last_page,
         data: [{REPORT_IDX, BUSINESS_CODE, BUSINESS_NAME, REPORT_DATE,
                 GRADE_VALUE, TARGET_STOCK_PRICES, OLD_TARGET_STOCK_PRICES,
                 CHANGE_STOCK_PRICES, REPORT_WRITER, OFFICE_NAME,
                 REPORT_TITLE, ...}, ...]}

Strategy:
    날짜 범위 기반 수집. report_idx 를 PK 로 사용하므로 동일 범위 재실행은
    멱등(ON CONFLICT DO UPDATE). 하루 단위 수집과 날짜 범위 백필을 모두
    지원하며, 공통 기반 함수(_fetch_all_pages → collect_date_range) 위에
    편의 함수(collect_one_day, backfill_date_range, backfill_one_year)를 쌓는
    구조.

Activation:
    HANKYUNG_ENABLED=true (또는 1/yes) 가 환경변수에 없으면 _enabled_check()
    가 RuntimeError 로 거부. docker-compose.gcp-db.yml 에서만 설정됨.
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

import httpx
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from src.db.repositories import log_collection
from src.utils.logger import logger

COLLECTOR_NAME = "consensus_hankyung"

_API_URL = "https://markets.hankyung.com/api/consensus/search/report"

# Nuxt JS 번들에 하드코딩된 정적 공개 토큰 (프론트엔드 API 키).
_BEARER_TOKEN = (
    "0ZdNlr7LrQoawewqweq78k6usasBsqhqSIaUarSTf8mxnHuQVh9CvKAfpUy94LhBmZMg"
)

_DEFAULT_HEADERS = {
    "Authorization": f"Bearer {_BEARER_TOKEN}",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://markets.hankyung.com/consensus",
    "Origin": "https://markets.hankyung.com",
}

_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# reportType=CO: 기업 리포트만 (종목코드 존재). 산업/시장/경제 리포트 제외.
_REPORT_TYPE = "CO"

# 페이지당 최대 레코드 수. API 측 cap 을 초과하면 서버가 기본값으로 내려줌.
_PER_PAGE = 100

# 요청 간 기본 딜레이 (초)
_DEFAULT_REQUEST_DELAY = 0.5

# 연속 실패 임계값 — 초과 시 abort
_CONSECUTIVE_FAIL_LIMIT = 10


# ---------------------------------------------------------------------------
# Activation guard
# ---------------------------------------------------------------------------


def _enabled_check() -> None:
    """HANKYUNG_ENABLED 환경변수 확인. 미설정 시 RuntimeError."""
    from src.config import get_hankyung_settings
    if not get_hankyung_settings().enabled:
        raise RuntimeError(
            "Hankyung consensus collector is disabled. "
            "Set HANKYUNG_ENABLED=true in the environment (docker-compose.gcp-db.yml) "
            "to enable. This collector is intended to run only in GCP-DB mode."
        )


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=15),
    reraise=True,
)
def _fetch_page(
    from_date: date,
    to_date: date,
    page: int = 1,
    per_page: int = _PER_PAGE,
) -> dict[str, Any]:
    """한경 컨센서스 API 한 페이지 가져오기 (재시도 포함).

    Returns:
        API 응답 dict: {current_page, per_page, total, last_page, data: [...]}
    Raises:
        httpx.HTTPStatusError: HTTP 4xx/5xx
    """
    params = {
        "fromDate": from_date.isoformat(),
        "toDate": to_date.isoformat(),
        "reportType": _REPORT_TYPE,
        "page": page,
        "per_page": per_page,
    }
    with httpx.Client(
        headers=_DEFAULT_HEADERS,
        timeout=_HTTP_TIMEOUT,
        follow_redirects=True,
    ) as client:
        resp = client.get(_API_URL, params=params)
    resp.raise_for_status()
    return resp.json()


def _extract_data(page_result: dict[str, Any]) -> list[dict[str, Any]]:
    """API 응답에서 레코드 리스트 추출.

    한경 API 의 quirk: 페이지 1은 data 가 list, 페이지 2 이상은
    {"5": {...}, "6": {...}} 형태의 dict (offset 인덱스 키) 로 내려온다.
    dict 인 경우 values() 를 사용해 레코드를 꺼낸다.
    """
    data = page_result.get("data") or []
    if isinstance(data, dict):
        return list(data.values())
    return list(data)


def _fetch_all_pages(
    from_date: date,
    to_date: date,
    request_delay: float = _DEFAULT_REQUEST_DELAY,
) -> list[dict[str, Any]]:
    """날짜 범위의 모든 페이지를 순회하여 raw 레코드 리스트 반환.

    페이지 1을 먼저 가져와 last_page 를 확인한 뒤 나머지 페이지를 순서대로
    수집. 각 페이지 사이에 request_delay 만큼 sleep.
    """
    first = _fetch_page(from_date, to_date, page=1)
    records: list[dict[str, Any]] = _extract_data(first)

    last_page = first.get("last_page", 1)
    total = first.get("total", len(records))
    logger.debug(
        f"  [{from_date}~{to_date}] total={total}, last_page={last_page}"
    )

    for page in range(2, last_page + 1):
        time.sleep(request_delay)
        page_data = _fetch_page(from_date, to_date, page=page)
        records.extend(_extract_data(page_data))

    return records


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_price(value: Any) -> float | None:
    """가격 문자열 → float. 파싱 불가 또는 0 이면 None."""
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if not s or s in {"-", "0", "N/A"}:
        return None
    try:
        v = float(s)
        return v if v != 0 else None
    except ValueError:
        return None


def _records_to_df(records: list[dict[str, Any]]) -> pd.DataFrame:
    """raw API 레코드 리스트를 hankyung_reports 컬럼 구조의 DataFrame 으로 변환."""
    if not records:
        return pd.DataFrame()

    rows = []
    for r in records:
        report_idx = r.get("REPORT_IDX")
        if not report_idx:
            continue
        rows.append({
            "report_idx":        int(report_idx),
            "business_code":     r.get("BUSINESS_CODE") or None,
            "business_name":     r.get("BUSINESS_NAME") or None,
            "report_date":       r.get("REPORT_DATE") or None,
            "grade_value":       r.get("GRADE_VALUE") or None,
            "target_price":      _parse_price(r.get("TARGET_STOCK_PRICES")),
            "prev_target_price": _parse_price(r.get("OLD_TARGET_STOCK_PRICES")),
            "change_price":      _parse_price(r.get("CHANGE_STOCK_PRICES")),
            "analyst":           r.get("REPORT_WRITER") or None,
            "office_name":       r.get("OFFICE_NAME") or None,
            "report_title":      (r.get("REPORT_TITLE") or "")[:500] or None,
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.where(pd.notna(df), None)
    return df


# ---------------------------------------------------------------------------
# Core collection function
# ---------------------------------------------------------------------------


def collect_date_range(
    from_date: date,
    to_date: date,
    request_delay: float = _DEFAULT_REQUEST_DELAY,
) -> int:
    """날짜 범위의 기업 리포트를 수집하여 DB에 upsert. Returns row count.

    재실행 안전: report_idx PK 기반 ON CONFLICT DO UPDATE.

    Args:
        from_date: 수집 시작일 (inclusive).
        to_date:   수집 종료일 (inclusive).
        request_delay: 페이지 요청 간 sleep (초).

    Returns:
        upsert 된 row 수. 해당 날짜 범위에 리포트가 없으면 0.
    """
    from src.db.repositories import upsert_hankyung_reports

    records = _fetch_all_pages(from_date, to_date, request_delay=request_delay)
    df = _records_to_df(records)
    if df.empty:
        return 0
    return upsert_hankyung_reports(df)


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------


def collect_one_day(
    target_date: date | None = None,
    skip_done: bool = True,
    request_delay: float = _DEFAULT_REQUEST_DELAY,
) -> int:
    """하루치 리포트 수집.

    Args:
        target_date: 수집 대상일. None 이면 오늘.
        skip_done:   이미 성공 로그가 있는 날짜는 건너뜀.
        request_delay: 페이지 요청 간 sleep (초).

    Returns:
        upsert 된 row 수.
    """
    _enabled_check()

    target_date = target_date or date.today()

    if skip_done and _is_date_done(target_date):
        logger.info(
            f"[{COLLECTOR_NAME}] {target_date} already collected — skip"
        )
        return 0

    t0 = time.time()
    try:
        rows = collect_date_range(target_date, target_date, request_delay=request_delay)
        dur = int((time.time() - t0) * 1000)
        log_collection(
            COLLECTOR_NAME, target_date, "success",
            rows_inserted=rows, duration_ms=dur,
        )
        logger.info(
            f"[{COLLECTOR_NAME}] {target_date}: {rows} rows upserted ({dur}ms)"
        )
        return rows
    except Exception as e:
        dur = int((time.time() - t0) * 1000)
        log_collection(
            COLLECTOR_NAME, target_date, "failed",
            error_message=str(e)[:500], duration_ms=dur,
        )
        raise


def backfill_date_range(
    from_date: date,
    to_date: date | None = None,
    skip_done: bool = True,
    request_delay: float = _DEFAULT_REQUEST_DELAY,
) -> dict[str, int]:
    """날짜 범위를 하루씩 순회하며 수집.

    영업일 구분 없이 모든 날짜를 시도함. 주말/공휴일은 API 가 0건을
    반환하고 success 로 기록되어 다음 resume 에서 skip 됨.

    Args:
        from_date:     수집 시작일 (inclusive).
        to_date:       수집 종료일 (inclusive). None 이면 오늘.
        skip_done:     이미 성공 로그가 있는 날짜는 건너뜀.
        request_delay: 페이지 요청 간 sleep (초).

    Returns:
        {ok, failed, skipped, total_rows}
    """
    _enabled_check()

    to_date = to_date or date.today()
    if from_date > to_date:
        raise ValueError(f"from_date({from_date}) > to_date({to_date})")

    total_days = (to_date - from_date).days + 1
    logger.info(
        f"[{COLLECTOR_NAME}] backfill {from_date} ~ {to_date} "
        f"({total_days} days), skip_done={skip_done}"
    )

    ok = 0
    failed = 0
    skipped = 0
    total_rows = 0
    consecutive_failures = 0

    current = from_date
    while current <= to_date:
        t0 = time.time()

        if skip_done and _is_date_done(current):
            skipped += 1
            current += timedelta(days=1)
            continue

        try:
            rows = collect_date_range(current, current, request_delay=request_delay)
            dur = int((time.time() - t0) * 1000)
            log_collection(
                COLLECTOR_NAME, current, "success",
                rows_inserted=rows, duration_ms=dur,
            )
            logger.info(
                f"  {current}: {rows} rows ({dur}ms)"
            )
            ok += 1
            total_rows += rows
            consecutive_failures = 0
        except Exception as e:
            dur = int((time.time() - t0) * 1000)
            log_collection(
                COLLECTOR_NAME, current, "failed",
                error_message=str(e)[:500], duration_ms=dur,
            )
            logger.error(f"  {current}: {e}")
            failed += 1
            consecutive_failures += 1
            if consecutive_failures >= _CONSECUTIVE_FAIL_LIMIT:
                logger.error(
                    f"Aborting: {consecutive_failures} consecutive failures. "
                    "Re-run to resume."
                )
                break

        current += timedelta(days=1)
        time.sleep(request_delay)

    logger.success(
        f"[{COLLECTOR_NAME}] done — ok: {ok}, failed: {failed}, "
        f"skipped: {skipped}, total rows: {total_rows:,}"
    )
    return {"ok": ok, "failed": failed, "skipped": skipped, "total_rows": total_rows}


def backfill_one_year(
    as_of_date: date | None = None,
    skip_done: bool = True,
    request_delay: float = _DEFAULT_REQUEST_DELAY,
) -> dict[str, int]:
    """1년치 리포트 백필.

    Args:
        as_of_date:    기준일. None 이면 오늘.
        skip_done:     이미 성공 로그가 있는 날짜는 건너뜀.
        request_delay: 페이지 요청 간 sleep (초).

    Returns:
        backfill_date_range 의 결과 dict.
    """
    _enabled_check()

    to_date = as_of_date or date.today()
    from_date = to_date - timedelta(days=365)
    logger.info(
        f"[{COLLECTOR_NAME}] 1-year backfill: {from_date} ~ {to_date}"
    )
    return backfill_date_range(
        from_date,
        to_date,
        skip_done=skip_done,
        request_delay=request_delay,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_date_done(target_date: date) -> bool:
    """해당 날짜에 이미 성공 수집 로그가 있으면 True."""
    from sqlalchemy import text
    from src.db.connection import session_scope
    with session_scope() as session:
        stmt = text("""
            SELECT 1 FROM collection_log
             WHERE collector  = :col
               AND target_date = :dt
               AND status      = 'success'
               AND symbol      IS NULL
             LIMIT 1
        """)
        row = session.execute(
            stmt, {"col": COLLECTOR_NAME, "dt": target_date}
        ).first()
        return row is not None
