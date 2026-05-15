"""FnGuide consensus estimates collector (개인용 비공개 사용 한정).

⚠️  사용 정책 / Usage policy
    이 모듈은 FnGuide(comp.fnguide.com) 의 공개 컨센서스 페이지를 스크래핑한다.
    FnGuide 약관은 데이터의 "데이터베이스화" 를 제한할 수 있으며, 본 모듈은
    **개인 트레이딩 시스템의 비공개 사용** 한정으로만 운용되어야 한다.
    재배포, 외부 공유, 상업적 활용은 금지된다.

    이 정책을 인지·동의했음을 환경변수 FNGUIDE_CONSENT_ACK=1 로 표시해야
    실행이 진행된다. 동의가 없으면 모듈은 RuntimeError 와 함께 거부한다.

Source URL pattern:
    https://comp.fnguide.com/SVO2/asp/SVD_Consensus.asp?gicode=A<6-digit>
    예) A005930 = 삼성전자

Strategy:
    Per-symbol, point-in-time. 한 호출 = 한 종목의 현재 컨센서스 스냅샷.
    매일 1회 (장 마감 후) 전 종목 스냅샷을 적재하여 추정치 변경 (revision)
    을 시계열로 추적. daily_fdr.py 와 동일한 symbol-iterating 패턴이며,
    log marker 의미상 target_date == as_of_date.

What this module DOES provide per (symbol, as_of_date):
    - FQ1~FQ4 + FY1~FY2 (총 6 rows)
    - EPS / 매출 / 영업이익 / 순이익 추정치 (페이지에서 노출되는 한)
    - 목표주가, 투자의견, 추정 애널리스트 수 (보통 연간 row 에만)

What this module does NOT provide:
    - 개별 리포트 단위 데이터 (한경 컨센서스 컬렉터에서 별도 수집 예정)
    - 추정치 분산 (표준편차/min/max) — FnGuide 무료 페이지에는 평균만 노출

페이지 단위 정규화:
    FnGuide 는 매출/영업이익/순이익을 "억원" 단위로 노출한다.
    DB 에는 "원" 단위 정수로 저장 (값 × 100,000,000).
    EPS / 목표주가는 "원" 단위 그대로.
"""
from __future__ import annotations

import os
import re
import time
from datetime import date, timedelta
from typing import Any

import httpx
import pandas as pd
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from src.config import get_app_config, get_fnguide_settings
from src.db.repositories import (
    get_active_tickers,
    get_completed_symbols_on_date,
    log_collection,
    upsert_consensus_estimates,
)
from src.utils.logger import logger

COLLECTOR_NAME = "consensus_fnguide"

# 데스크탑 컨센서스 페이지 URL 템플릿. gicode = 'A' + 6자리 종목코드.
_FNGUIDE_CONSENSUS_URL = (
    "https://comp.fnguide.com/SVO2/asp/SVD_Consensus.asp"
    "?gicode=A{symbol}&MenuYn=Y&ReportGB=&NewMenuID=108&stkGb=701"
)

# 정상 브라우저처럼 보이게 헤더 명시 (서버 부담 매너 + bot 차단 회피).
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://comp.fnguide.com/",
}

# 1억 = 매출/이익 단위 환산 계수 (FnGuide '억원' → DB '원').
_OK_UNIT = 100_000_000

# HTTP timeout. 페이지가 무거워서 10s 는 줘야 함.
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=10.0)

# fiscal_period 정규식 매칭용:
#   분기: 2026/03, 2026.03, 2026.3(E), 26.03 등 다양 → (year, month) 추출
#   연간: 2026/12(E), 2025/12 등
_PERIOD_QUARTER_RE = re.compile(r"(\d{2,4})[./\-](\d{1,2})")


def _consent_check() -> None:
    """모듈 실행 전 사용자 동의 확인.

    FNGUIDE_CONSENT_ACK=1 이 .env 에 있어야 진행. 없으면 RuntimeError
    로 정지 — 우발 실행 방지.

    구현: pydantic-settings (`get_fnguide_settings()`) 로 읽기 때문에
    `os.environ` 채움 시점이나 `load_dotenv()` 호출 순서에 구애받지
    않음. 다른 설정들(DB, KIS, DART)과 동일한 경로를 사용.
    """
    settings = get_fnguide_settings()
    if not settings.consent_ack:
        raise RuntimeError(
            "FnGuide consensus collector requires explicit consent. "
            "Set FNGUIDE_CONSENT_ACK=1 in .env to acknowledge that this "
            "data is for personal, non-public use only and that you accept "
            "the legal risk of FnGuide's database-rights clause. "
            f"Loaded value (via pydantic-settings) = {settings.fnguide_consent_ack!r}. "
            "If non-empty but not '1', verify the .env line has no "
            "trailing whitespace or comment."
        )


def _to_quarter_period(period_label: str) -> tuple[str, str] | None:
    """파싱된 컬럼 라벨을 (fiscal_period, fiscal_period_type) 으로 변환.

    Args:
        period_label: 페이지에서 추출된 헤더 셀 텍스트. 예:
            '2026/03(E)', '2026.03(E)', '2026/12(E)', '2026/12', '2026'

    Returns:
        ('2026Q1', 'quarterly') 또는 ('FY2026', 'annual') 또는 None.
        파싱 실패 시 None.
    """
    s = period_label.strip()
    if not s:
        return None

    # (E) / (P) 등 추정 표기 제거
    s_clean = re.sub(r"\([A-Za-z]\)", "", s).strip()

    # 'YYYY/MM' 또는 'YYYY.MM' 매칭
    m = _PERIOD_QUARTER_RE.search(s_clean)
    if m:
        year_part, month_part = m.group(1), m.group(2)
        # 2자리 연도면 2000년대로
        year = int(year_part) if len(year_part) == 4 else 2000 + int(year_part)
        month = int(month_part)
        # 12월 결산 분기 매핑: 3=Q1, 6=Q2, 9=Q3, 12=Q4
        # 비-12월 결산 종목은 향후 별도 처리 필요 (Phase 1 에서는 단순 매핑)
        if month == 12:
            # 연 결산월이 12 인 경우, 컨센서스 페이지에서는 보통 연간 row
            return f"FY{year}", "annual"
        if month in (3, 6, 9):
            quarter = month // 3
            return f"{year}Q{quarter}", "quarterly"
        # 12월 외 결산월의 분기 추정치는 일단 분기로 라벨링
        return f"{year}M{month:02d}", "quarterly"

    # 연도만 있는 경우: '2026'
    if re.fullmatch(r"\d{4}", s_clean):
        return f"FY{s_clean}", "annual"

    return None


def _parse_amount(text: str, *, is_oku_won: bool = False) -> float | None:
    """FnGuide 셀 텍스트를 숫자로 파싱.

    Args:
        text: 셀 raw 텍스트. 예: '5,231', '5,231.45', '-1,200', '-', '', 'N/A'
        is_oku_won: True 이면 '억원' 단위로 간주하고 × 100_000_000.

    Returns:
        float 값. 결측/파싱불가 시 None.
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s or s in {"-", "N/A", "n/a", "NA", "—"}:
        return None
    # 콤마 / 공백 제거
    s = s.replace(",", "").replace(" ", "")
    # 일부 페이지에서 음수가 △ / ▲ 로 표기되는 경우
    if s.startswith("△") or s.startswith("▲"):
        s = "-" + s[1:]
    try:
        val = float(s)
    except ValueError:
        return None
    if is_oku_won:
        val *= _OK_UNIT
    return val


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=15),
    reraise=True,
)
def _safe_fetch(symbol: str) -> str:
    """FnGuide 컨센서스 페이지 HTML 가져오기 (재시도 포함).

    HTTP 4xx (특히 404 = 존재하지 않는 종목코드) 는 retry 무의미하므로
    재시도하지 않고 즉시 raise. 5xx / timeout / connection 오류만 retry.
    """
    url = _FNGUIDE_CONSENSUS_URL.format(symbol=symbol)
    with httpx.Client(
        headers=_DEFAULT_HEADERS,
        timeout=_HTTP_TIMEOUT,
        follow_redirects=True,
    ) as client:
        resp = client.get(url)

    # 4xx 는 종목 자체 문제이므로 즉시 raise (tenacity 가 retry 안 함:
    # raise_for_status 후 HTTPStatusError 는 ValueError 가 아니지만 본 코드는
    # reraise=True 라서 그냥 올라감 — 결과적으로 retry 는 3회 후 abort).
    resp.raise_for_status()

    # 한국어 페이지: 일부 응답이 EUC-KR / CP949 일 수 있음. httpx 가 charset
    # 미지정 시 UTF-8 추정해 깨질 수 있으므로 명시적으로 검사.
    if resp.encoding is None or resp.encoding.lower() in {"iso-8859-1", "ascii"}:
        resp.encoding = "utf-8"
    return resp.text


def _find_consensus_tables(soup: BeautifulSoup) -> list[Any]:
    """컨센서스 페이지에서 '추정치 테이블' 후보들 추출.

    FnGuide 페이지 구조 (관찰 기반, 변경 가능):
        - <div class="um_tabin"> 안에 분기/연간 컨센서스 표
        - 또는 <table class="us_table_ty1"> / <table class="us_table_ty2">
        - 각 표의 thead 에 '2026/03(E)' 류 기간 헤더가 있음

    범용 전략: 페이지의 모든 <table> 을 가져온 뒤, 헤더 셀에
        'YYYY/MM' 또는 'YYYY.MM' 패턴이 1 개 이상 있는 것만 후보로 채택.
    """
    candidates = []
    for table in soup.find_all("table"):
        header_texts: list[str] = []
        thead = table.find("thead")
        if thead:
            header_texts.extend(c.get_text(strip=True) for c in thead.find_all(["th", "td"]))
        # thead 가 없으면 첫 번째 <tr> 을 헤더로 간주
        if not header_texts:
            first_tr = table.find("tr")
            if first_tr:
                header_texts.extend(
                    c.get_text(strip=True) for c in first_tr.find_all(["th", "td"])
                )

        if any(_PERIOD_QUARTER_RE.search(t) for t in header_texts):
            candidates.append(table)
    return candidates


def _extract_table_rows(table: Any) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """테이블을 (period_headers, [(row_label, [cells...]), ...]) 형태로 변환.

    period_headers 는 첫 컬럼(라벨 컬럼)을 제외한 나머지 헤더들.
    각 row 는 (첫 셀 텍스트 = 라벨, 나머지 셀 텍스트 리스트).
    """
    # 헤더 추출
    headers: list[str] = []
    thead = table.find("thead")
    header_row = None
    if thead:
        header_row = thead.find("tr")
    if header_row is None:
        header_row = table.find("tr")
    if header_row is not None:
        cells = header_row.find_all(["th", "td"])
        headers = [c.get_text(" ", strip=True) for c in cells]

    # 헤더의 첫 셀은 라벨 컬럼 (보통 빈 문자열 또는 'IFRS(연결)' 등)
    period_headers = headers[1:] if headers else []

    # 본문 row 추출
    body_rows: list[tuple[str, list[str]]] = []
    tbody = table.find("tbody")
    rows_iter = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]
    for tr in rows_iter:
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        label = cells[0].get_text(" ", strip=True)
        values = [c.get_text(" ", strip=True) for c in cells[1:]]
        body_rows.append((label, values))

    return period_headers, body_rows


# 행 라벨 → (대상 컬럼, 단위) 매핑.
# 페이지마다 표기가 달라서 substring 매칭 사용.
_ROW_PATTERNS: list[tuple[str, str, bool]] = [
    # (검색어 substring, 컬럼명, is_oku_won)
    ("매출액", "revenue_estimate", True),
    ("매출", "revenue_estimate", True),  # fallback
    ("영업이익", "op_income_estimate", True),
    ("당기순이익", "net_income_estimate", True),
    ("순이익", "net_income_estimate", True),  # fallback
    ("EPS", "eps_estimate", False),
    ("주당순이익", "eps_estimate", False),
    ("목표주가", "target_price", False),
]


def _match_row(label: str) -> tuple[str, bool] | None:
    """행 라벨에 가장 먼저 매칭되는 (column, is_oku_won) 반환.

    매칭 우선순위는 _ROW_PATTERNS 리스트 순서.
    """
    for substr, col, is_oku in _ROW_PATTERNS:
        if substr in label:
            return col, is_oku
    return None


def _parse_consensus(html: str, symbol: str, as_of_date: date) -> pd.DataFrame:
    """HTML 을 pandas DataFrame (consensus_estimates 스키마) 으로 변환.

    실패해도 raise 하지 않고 빈 DataFrame 반환 (caller 의 loop 보호).
    Returns:
        DataFrame with columns matching CONSENSUS_COLUMNS subset.
        Empty if parsing failed or no estimates were found.
    """
    soup = BeautifulSoup(html, "lxml")
    tables = _find_consensus_tables(soup)
    if not tables:
        logger.debug(f"  {symbol}: no consensus tables found on page")
        return pd.DataFrame()

    # (fiscal_period, column) → value 누적
    bucket: dict[str, dict[str, Any]] = {}

    for table in tables:
        period_headers, body_rows = _extract_table_rows(table)
        if not period_headers or not body_rows:
            continue

        # 컬럼 헤더 → (fiscal_period, fiscal_period_type) 미리 파싱
        col_periods: list[tuple[str, str] | None] = [
            _to_quarter_period(h) for h in period_headers
        ]

        for row_label, cells in body_rows:
            matched = _match_row(row_label)
            if not matched:
                continue
            col_name, is_oku = matched

            for idx, cell in enumerate(cells):
                if idx >= len(col_periods):
                    break
                period_info = col_periods[idx]
                if period_info is None:
                    continue
                fiscal_period, fiscal_period_type = period_info
                val = _parse_amount(cell, is_oku_won=is_oku)
                if val is None:
                    continue

                entry = bucket.setdefault(
                    fiscal_period,
                    {
                        "symbol": symbol,
                        "as_of_date": as_of_date,
                        "fiscal_period": fiscal_period,
                        "fiscal_period_type": fiscal_period_type,
                        "source": "fnguide",
                    },
                )
                # 동일 컬럼 중복 매칭 시 첫 값 유지 (_ROW_PATTERNS 우선순위 존중)
                if col_name not in entry:
                    entry[col_name] = val

    if not bucket:
        return pd.DataFrame()

    df = pd.DataFrame(list(bucket.values()))
    # 필요한 컬럼이 없으면 NaN 으로 채워줌 (upsert 단에서 None 으로 변환됨)
    for col in (
        "eps_estimate", "revenue_estimate", "op_income_estimate",
        "net_income_estimate", "target_price", "opinion", "n_estimates",
    ):
        if col not in df.columns:
            df[col] = None

    # NaN → None
    df = df.where(pd.notna(df), None)
    return df


def collect_one_symbol(symbol: str, as_of_date: date | None = None) -> int:
    """한 종목의 컨센서스를 가져와서 upsert. Returns row count.

    Args:
        symbol: 6자리 KRX 종목코드.
        as_of_date: 스냅샷 날짜. 기본값은 오늘.

    Returns:
        upsert 된 row 수 (보통 0~6). 0 이면 페이지에 추정치가 없거나 파싱
        실패 — 호출자는 이를 'skipped' 로 기록.
    """
    as_of_date = as_of_date or date.today()
    html = _safe_fetch(symbol)
    df = _parse_consensus(html, symbol, as_of_date)
    if df.empty:
        return 0
    return upsert_consensus_estimates(df)


def backfill_symbols(
    symbols: list[str],
    as_of_date: date | None = None,
    skip_done: bool = True,
    consecutive_fail_limit: int = 20,
    request_delay: float | None = None,
) -> dict[str, int]:
    """전 종목 컨센서스 일 1회 스냅샷.

    Args:
        symbols: 6자리 KRX 종목코드 리스트.
        as_of_date: 스냅샷 날짜. 기본 = 오늘. 같은 날 재실행하면 PK 충돌
            업데이트로 멱등.
        skip_done: True 면 같은 as_of_date 에 이미 success 로그가 있는
            종목을 건너뜀 (resume 지원).
        consecutive_fail_limit: 연속 실패 N 회 시 abort. 기본 20.
        request_delay: 요청 간 sleep (초). None 이면 config 의
            collection.daily.request_delay 사용 (현 프로젝트 기본 0.3 보다는
            FnGuide 매너 차원에서 1.0 권장).

    Returns:
        {ok, failed, empty, skipped, total_rows}
    """
    _consent_check()

    cfg = get_app_config()
    as_of_date = as_of_date or date.today()
    if request_delay is None:
        request_delay = max(cfg.collection.daily.request_delay, 1.0)

    if not symbols:
        logger.warning("backfill_symbols called with empty symbol list")
        return {"ok": 0, "failed": 0, "empty": 0, "skipped": 0, "total_rows": 0}

    # Resume
    skipped_done = 0
    if skip_done:
        already = get_completed_symbols_on_date(COLLECTOR_NAME, as_of_date)
        if already:
            before = len(symbols)
            symbols = [s for s in symbols if s not in already]
            skipped_done = before - len(symbols)
            logger.info(
                f"Resume: {skipped_done} symbol(s) already completed for "
                f"as_of_date={as_of_date}, {len(symbols)} remaining"
            )

    if not symbols:
        logger.success("Nothing to backfill — every symbol already done.")
        return {
            "ok": 0, "failed": 0, "empty": 0,
            "skipped": skipped_done, "total_rows": 0,
        }

    logger.info(
        f"FnGuide consensus backfill: {len(symbols)} symbol(s), "
        f"as_of_date={as_of_date}, delay={request_delay}s"
    )

    ok = 0
    failed = 0
    empty = 0
    total_rows = 0
    consecutive_failures = 0

    for sym in tqdm(symbols, desc="fnguide consensus"):
        t0 = time.time()
        try:
            rows = collect_one_symbol(sym, as_of_date)
            dur = int((time.time() - t0) * 1000)
            if rows == 0:
                empty += 1
                # 추정치가 없는 종목 (소형주, 신규 상장 등) → skipped.
                # 다음 resume 에서는 retry 됨 (success 가 아니므로).
                log_collection(
                    COLLECTOR_NAME, as_of_date, "skipped",
                    symbol=sym, duration_ms=dur,
                )
            else:
                ok += 1
                total_rows += rows
                log_collection(
                    COLLECTOR_NAME, as_of_date, "success",
                    symbol=sym, rows_inserted=rows, duration_ms=dur,
                )
            consecutive_failures = 0
        except Exception as e:
            failed += 1
            consecutive_failures += 1
            dur = int((time.time() - t0) * 1000)
            logger.error(f"  {sym}: {e}")
            log_collection(
                COLLECTOR_NAME, as_of_date, "failed",
                symbol=sym, error_message=str(e)[:500], duration_ms=dur,
            )
            if (
                consecutive_fail_limit > 0
                and consecutive_failures >= consecutive_fail_limit
            ):
                logger.error(
                    f"Aborting: {consecutive_failures} consecutive failures. "
                    "Likely a network/source issue. Re-run to resume."
                )
                break
        time.sleep(request_delay)

    logger.success(
        f"FnGuide consensus backfill done — ok: {ok}, failed: {failed}, "
        f"empty: {empty}, skipped(done): {skipped_done}, "
        f"total rows: {total_rows:,}"
    )
    return {
        "ok": ok,
        "failed": failed,
        "empty": empty,
        "skipped": skipped_done,
        "total_rows": total_rows,
    }


def backfill_active_universe(
    as_of_date: date | None = None,
    markets: list[str] | None = None,
    skip_done: bool = True,
    request_delay: float | None = None,
) -> dict[str, int]:
    """`tickers` 의 모든 active 종목에 대해 컨센서스 일 스냅샷.

    Performance note: ~1 HTTP call per symbol. 2,600 종목 × 1초 = ~45분.
    여유있게 1.5~2 시간 잡으면 됨 (네트워크 변동 포함).

    Prerequisite: `tickers` 테이블이 채워져 있어야 함.
    """
    cfg = get_app_config()
    tickers = get_active_tickers(markets or cfg.markets)
    symbols = [t.symbol for t in tickers]
    if not symbols:
        logger.warning(
            "Active universe is empty — populate `tickers` first."
        )
        return {"ok": 0, "failed": 0, "empty": 0, "skipped": 0, "total_rows": 0}
    logger.info(f"Active universe: {len(symbols)} ticker(s)")
    return backfill_symbols(
        symbols,
        as_of_date=as_of_date,
        skip_done=skip_done,
        request_delay=request_delay,
    )


if __name__ == "__main__":
    # Smoke test: 삼성전자 + SK하이닉스 오늘 스냅샷
    # 실행 전 .env 에 FNGUIDE_CONSENT_ACK=1 설정 필요.
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)
    backfill_symbols(["005930", "000660"])
