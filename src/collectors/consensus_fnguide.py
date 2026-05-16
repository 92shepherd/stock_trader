"""FnGuide consensus estimates collector (개인용 비공개 사용 한정).

⚠️  사용 정책 / Usage policy
    이 모듈은 FnGuide(comp.fnguide.com) 의 공개 컨센서스 데이터를 가져온다.
    FnGuide 약관은 데이터의 "데이터베이스화" 를 제한할 수 있으며, 본 모듈은
    **개인 트레이딩 시스템의 비공개 사용** 한정으로만 운용되어야 한다.
    재배포, 외부 공유, 상업적 활용은 금지된다.

    이 정책을 인지·동의했음을 환경변수 FNGUIDE_CONSENT_ACK=1 로 표시해야
    실행이 진행된다. 동의가 없으면 모듈은 RuntimeError 와 함께 거부한다.

Source endpoint pattern:
    SVD_Consensus.asp 페이지가 표시하는 컨센서스 표는 JS 가 별도 JSON 을
    가져와 채워넣는 구조다. 따라서 HTML 페이지만 받으면 본문 셀이 비어
    있다 (관찰: 2026-05 기준 thead/tbody id="theadcontent1"/"bodycontent1"
    가 빈 채로 내려온다). 직접 JSON 엔드포인트를 호출한다:

        https://comp.fnguide.com/SVO2/json/data/01_06/01_A{symbol}_{AQ}_D.json

    AQ = 'A' (annual) | 'Q' (quarterly). RptGb='D' 가 컨센서스 + 실적
    혼합 뷰 (페이지 기본).

Strategy:
    Per-symbol, point-in-time. 한 종목당 2 회 HTTP (A + Q). 매일 1회
    (장 마감 후) 전 종목 스냅샷을 적재하여 추정치 변경 (revision) 을 시계열
    로 추적. log marker 의미상 target_date == as_of_date.

What this module DOES provide per (symbol, as_of_date):
    - 연간 행 3 개 (직전 2 년 actual + 향후 3 년 estimate, FnGuide 가
      노출하는 한) + 분기 행 6 개 (직전 3 분기 actual + 향후 3 분기
      estimate). 페이지가 보여주는 모든 컬럼을 그대로 저장한다.
    - EPS / 매출 / 영업이익 / 당기순이익 추정치

What this module does NOT provide:
    - 목표주가, 투자의견, 추정 애널리스트 수 — FnGuide 가 별도 JSON
      엔드포인트로 제공. (향후 별도 작업으로 추가)
    - 추정치 분산 (표준편차/min/max) — 무료 페이지에 평균만 노출.

단위 정규화:
    FnGuide JSON 은 매출/영업이익/순이익을 "억원" 단위로 노출한다.
    DB 에는 "원" 단위 정수로 저장 (값 × 100,000,000).
    EPS 는 "원" 단위 그대로.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date
from typing import Any

import httpx
import pandas as pd
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

# JSON 엔드포인트 — SVD_Consensus.asp 페이지의 JS 가 호출하는 동일 경로.
# AQ ∈ {'A','Q'} (annual / quarterly). 'D' = 페이지 기본 RptGb (혼합 뷰).
_FNGUIDE_JSON_URL = (
    "https://comp.fnguide.com/SVO2/json/data/01_06/01_A{symbol}_{aq}_D.json"
)

# Referer 는 같은 페이지로 위장 (FnGuide 가 origin 점검할 가능성 대비).
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://comp.fnguide.com/SVO2/asp/SVD_Consensus.asp",
}

# 1억 = 매출/이익 단위 환산 계수 (FnGuide '억원' → DB '원').
_OK_UNIT = 100_000_000

# HTTP timeout. JSON 은 작지만 회선 변동 여유.
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=10.0)

# 컨센서스가 없는 종목에 대해 FnGuide 가 내려주는 빈 페이로드는 ~1KB.
# 정상 데이터(comp 23행)는 3KB 이상. 안전 임계값.
_EMPTY_PAYLOAD_MAX_BYTES = 1500

# 페이지가 표시하는 6 개 데이터 컬럼 (헤더 행에서도 동일 키 사용).
_PERIOD_KEYS = ("D_2", "D_3", "D_4", "D_5", "D_6", "D_7")

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
def _fetch_consensus_json(symbol: str, aq_gb: str) -> dict | None:
    """FnGuide 컨센서스 JSON 한 본 가져오기 (재시도 포함).

    Args:
        symbol: 6자리 KRX 종목코드.
        aq_gb: 'A' = 연간(annual), 'Q' = 분기(quarterly).

    Returns:
        파싱된 dict ({"comp": [...]}), 또는 데이터가 비었을 때 None.
        HTTP 4xx/5xx 는 raise — caller 가 종목 단위로 트래핑.

    응답 인코딩: 본문 앞에 UTF-8 BOM 이 붙어 있어 `utf-8-sig` 로 디코드.
    데이터가 없는 종목/리포트조합은 동일 200 응답이지만 1KB 미만이고
    JSON 본체에는 헤더 한 행만 들어있다 (관찰).
    """
    url = _FNGUIDE_JSON_URL.format(symbol=symbol, aq=aq_gb)
    with httpx.Client(
        headers=_DEFAULT_HEADERS,
        timeout=_HTTP_TIMEOUT,
        follow_redirects=True,
    ) as client:
        resp = client.get(url)
    resp.raise_for_status()

    # FnGuide 가 일부 "데이터 없음" 응답에 mojibake 한국어를 섞어 내려보내는
    # 경우가 있어, JSON 파싱 실패 = 데이터 없음으로 해석.
    text = resp.content.decode("utf-8-sig", errors="replace")
    if len(resp.content) < _EMPTY_PAYLOAD_MAX_BYTES:
        # 빠른 거름. 1.5KB 미만이면 정상 23행 페이로드가 아님.
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
        comp = obj.get("comp") or []
        if len(comp) <= 1:
            return None
        return obj

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


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


def _consume_payload(
    obj: dict | None,
    symbol: str,
    as_of_date: date,
    bucket: dict[str, dict[str, Any]],
) -> None:
    """JSON 한 본 (A 또는 Q) 을 bucket 에 머지.

    bucket key = fiscal_period (e.g. '2026Q2', 'FY2026'). 동일 period 가
    A/Q 양쪽에 들어있을 수는 거의 없지만 (연/분기 라벨링이 다르므로),
    있어도 첫 값을 유지하도록 setdefault.
    """
    if not obj:
        return
    comp = obj.get("comp") or []
    if len(comp) < 2:
        return

    # 첫 행 (SORT_ORDER=0, ACCOUNT_NM='항목') 이 D_2..D_7 의 기간 라벨을
    # 가진다. 라벨 예: '2025/06', '2026/03(P)', '2026/06(E)', '2026/12'
    header = comp[0]
    col_periods: list[tuple[str, str] | None] = [
        _to_quarter_period(str(header.get(k, ""))) for k in _PERIOD_KEYS
    ]

    for row in comp[1:]:
        label = str(row.get("ACCOUNT_NM", ""))
        matched = _match_row(label)
        if not matched:
            # GB=1 (전년동기대비), GB=2 (컨센서스대비) 행은 라벨이 달라
            # 자연스럽게 걸러진다. 자산/부채 등 비대상 행도 동일.
            continue
        col_name, is_oku = matched

        for idx, key in enumerate(_PERIOD_KEYS):
            period_info = col_periods[idx]
            if period_info is None:
                continue
            fiscal_period, fiscal_period_type = period_info
            val = _parse_amount(row.get(key), is_oku_won=is_oku)
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


def _parse_consensus(
    obj_a: dict | None,
    obj_q: dict | None,
    symbol: str,
    as_of_date: date,
) -> pd.DataFrame:
    """FnGuide JSON (annual + quarterly) 을 consensus_estimates 행들로 변환.

    실패해도 raise 하지 않고 빈 DataFrame 반환 (caller 의 loop 보호).
    """
    bucket: dict[str, dict[str, Any]] = {}
    _consume_payload(obj_a, symbol, as_of_date, bucket)
    _consume_payload(obj_q, symbol, as_of_date, bucket)

    if not bucket:
        return pd.DataFrame()

    df = pd.DataFrame(list(bucket.values()))
    # 필요한 컬럼이 없으면 NaN 으로 채워줌 (upsert 단에서 None 으로 변환됨).
    # target_price/opinion/n_estimates 는 별 엔드포인트 — 현재는 항상 None.
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
        upsert 된 row 수 (보통 0~9: 연간 3 + 분기 6). 0 이면 FnGuide 에
        해당 종목 컨센서스가 없음 — 호출자는 이를 'skipped' 로 기록.
    """
    as_of_date = as_of_date or date.today()
    obj_a = _fetch_consensus_json(symbol, "A")
    obj_q = _fetch_consensus_json(symbol, "Q")
    df = _parse_consensus(obj_a, obj_q, symbol, as_of_date)
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
