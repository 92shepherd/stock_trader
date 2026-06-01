"""KR 종목 마스터 수집기 — DART 기반.

Source:
    1) `dart_corp_codes` 테이블 (선행 수집 필요. /collect/dart/corp-codes 실행).
       stock_code IS NOT NULL 행 = DART 가 인지하는 모든 KR 상장사 + corp_code 매핑.
    2) DART /api/company.json (회사개황). corp_code 입력 → 회사 메타 단건 조회.
       OpenDartReader: `dart.company(corp_code)` 한 줄 호출.

DART /api/company.json 에서 사용하는 필드:
    corp_cls     — 시장 구분 (Y=KOSPI, K=KOSDAQ, N=KONEX, E=기타)
    induty_code  — KSIC 한국표준산업분류 5자리 업종코드
    est_dt       — 회사 설립일 (YYYYMMDD, 상장일과는 다름)
    acc_mt       — 결산월 (예: '12')
    corp_name    — 한글 회사명 (dart_corp_codes 와 동일하지만 정합성 검증용)

설계:
    티커는 quasi-static (IPO / 상장폐지 / 종목명변경 시에만 변함). 매일 갱신
    불필요. 신규 종목만 골라 회사개황을 1회 호출.

    refresh_tickers_from_dart() 흐름:
      1. dart_corp_codes 에서 stock_code 가 있는 행 로딩
      2. tickers 와 차집합:
           - 신규 (DART 에는 있는데 tickers 에 없거나 delisted=True) → fetch 대상
           - 사라짐 (tickers 에는 있는데 DART 에 없음)              → delisted 마킹
      3. 신규 종목만 dart.company(corp_code) 호출 → 메타 수집
      4. tickers 테이블에 upsert + 사라진 종목 delisted 마킹

비용 분석:
    - 부트스트랩 1회: ~2,600 calls (DART 일일 한도 10,000 의 26%)
    - 이후 IPO 발생당 1 call (주당 0~5건)
    - --force-all 사용 시: 약 2,600 calls (스키마 변경 후 재수집용)

CLI:
    python -m src.collectors.tickers_kr                # 신규 종목만 처리 (정상 운영)
    python -m src.collectors.tickers_kr --force-all    # 전체 메타 재수집
    python -m src.collectors.tickers_kr --max-new 100  # 신규 처리 상한 (디버그)
    python -m src.collectors.tickers_kr --delay 0.3    # 호출 간 sleep 조정
"""
from __future__ import annotations

import argparse
import time
from datetime import date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from tenacity import retry, stop_after_attempt, wait_exponential

from src.collectors.dart_common import get_dart_client
from src.db.connection import session_scope
from src.db.models import DartCorpCode, Ticker
from src.utils.logger import logger

COLLECTOR_NAME = "tickers_kr"

# DART 호출 간 sleep. personal-tier 한도가 10,000 calls/day 지만 짧은 시간 내
# 연속 호출 시 일시적 5xx 가 발생할 수 있어 보수적으로 0.15s.
DEFAULT_REQUEST_DELAY = 0.15


# ---------------------------------------------------------------------------
# 정규화 헬퍼
# ---------------------------------------------------------------------------


def _parse_yyyymmdd(s: Any) -> date | None:
    """DART 의 'YYYYMMDD' 문자열 → date. 잘못된 입력은 None."""
    if not s or not isinstance(s, (str, int)):
        return None
    s = str(s).strip()
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return None


def _normalize_acc_mt(v: Any) -> str | None:
    """'12' / '6' / 12 → '12' / '06'. 비숫자는 None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or not s.isdigit():
        return None
    return s.zfill(2)[:2]


def _normalize_corp_cls(v: Any) -> str | None:
    """Y/K/N/E 만 허용. 그 외는 None (CHECK constraint 위반 방지)."""
    if v is None:
        return None
    s = str(v).strip().upper()
    return s if s in ("Y", "K", "N", "E") else None


def _normalize_induty(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.isdigit() else None


# ---------------------------------------------------------------------------
# DART 호출
# ---------------------------------------------------------------------------


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _fetch_company_info(dart, corp_code: str) -> dict[str, Any] | None:
    """DART /api/company.json 호출 → 정규화 dict.

    OpenDartReader 의 `dart.company(corp_code)` 반환 형태는 버전에 따라
    DataFrame / Series / dict 가 섞여 있어 모두 처리.

    Returns:
        {corp_cls, induty_code, est_dt, acc_mt, corp_name_dart} or None.
    """
    try:
        res = dart.company(corp_code)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[tickers_kr] company({corp_code}) failed: {e}")
        return None

    if res is None:
        return None
    if isinstance(res, pd.DataFrame):
        if res.empty:
            return None
        row = res.iloc[0].to_dict()
    elif isinstance(res, pd.Series):
        row = res.to_dict()
    elif isinstance(res, dict):
        row = res
    else:
        logger.warning(
            f"[tickers_kr] unexpected response type for {corp_code}: {type(res)}"
        )
        return None

    return {
        "corp_cls": _normalize_corp_cls(row.get("corp_cls")),
        "induty_code": _normalize_induty(row.get("induty_code")),
        "est_dt": _parse_yyyymmdd(row.get("est_dt")),
        "acc_mt": _normalize_acc_mt(row.get("acc_mt")),
        "corp_name_dart": (
            str(row.get("corp_name") or "").strip() or None
        ),
    }


# ---------------------------------------------------------------------------
# DB 로딩
# ---------------------------------------------------------------------------


def _load_dart_listed() -> dict[str, tuple[str, str]]:
    """dart_corp_codes 에서 상장 종목만 추출.

    Returns:
        {stock_code(6자리): (corp_code, corp_name)} 매핑.
        중복된 stock_code 가 있으면 (드물게 corp_code 가 정정 시 발생) 가장
        최근 modify_date 의 row 가 채택됨.
    """
    with session_scope() as session:
        rows = session.execute(
            select(
                DartCorpCode.stock_code,
                DartCorpCode.corp_code,
                DartCorpCode.corp_name,
                DartCorpCode.modify_date,
            )
            .where(DartCorpCode.stock_code.is_not(None))
            .order_by(DartCorpCode.modify_date.asc().nulls_first())
        ).all()

    out: dict[str, tuple[str, str]] = {}
    for sc, cc, name, _md in rows:
        if not sc:
            continue
        sc = sc.strip().zfill(6)
        out[sc] = (cc, name)  # 정렬상 asc → 마지막 덮어쓰기가 최신
    return out


def _load_existing_tickers() -> dict[str, dict[str, Any]]:
    """tickers 의 현재 symbol 별 상태 (delisted 여부, corp_code 채움 여부)."""
    with session_scope() as session:
        rows = session.execute(
            select(Ticker.symbol, Ticker.delisted, Ticker.corp_code)
        ).all()
    return {
        sym: {"delisted": bool(d), "corp_code": cc}
        for sym, d, cc in rows
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def refresh_tickers_from_dart(
    *,
    force_all: bool = False,
    max_new: int | None = None,
    request_delay: float | None = None,
) -> dict[str, int]:
    """tickers 테이블을 dart_corp_codes + DART /api/company.json 으로 갱신.

    Args:
        force_all: True 면 dart_corp_codes 의 모든 상장 종목에 대해 메타 재수집.
                   부트스트랩(스키마 변경 직후) 또는 풀 백필 용도.
                   False(기본) 면 tickers 에 없는 신규 종목만 처리.
        max_new:   한 번 실행에서 처리할 신규 종목 최대치. DART 일일 한도를
                   여러 번에 나눠 쓰고 싶을 때 유용. None 이면 무제한.
        request_delay: company.json 호출 간 sleep(초). None 이면
                       DEFAULT_REQUEST_DELAY 사용.

    Returns:
        {fetched_meta, inserted, updated, delisted_marked, fetch_failed,
         skipped_existing} 카운트 dict.
    """
    delay = request_delay if request_delay is not None else DEFAULT_REQUEST_DELAY
    today = date.today()

    logger.info("[tickers_kr] Step 1/3: dart_corp_codes 에서 상장 종목 로딩")
    dart_listed = _load_dart_listed()
    logger.info(f"  DART 상장 종목: {len(dart_listed)}")

    existing = _load_existing_tickers()
    logger.info(f"  기존 tickers row: {len(existing)}")

    dart_symbols = set(dart_listed.keys())
    existing_symbols = set(existing.keys())

    # 신규 = (DART 에 있고 tickers 에 없음) ∪ (있어도 delisted=True 인 부활 종목)
    new_symbols = (dart_symbols - existing_symbols) | {
        s for s in (dart_symbols & existing_symbols)
        if existing[s]["delisted"]
    }
    # 사라짐 = tickers 에는 있지만 DART 에 없는 symbol (delisted 후보)
    missing_symbols = existing_symbols - dart_symbols

    if force_all:
        targets = sorted(dart_symbols)
        logger.info(
            f"[tickers_kr] --force-all: 전체 {len(targets)} 종목 메타 재수집"
        )
    else:
        targets = sorted(new_symbols)
        logger.info(
            f"[tickers_kr] 신규 {len(new_symbols)}, 사라짐 {len(missing_symbols)}"
        )

    if max_new is not None and len(targets) > max_new:
        logger.warning(
            f"  --max-new={max_new} 적용: 처리 대상 {len(targets)} → {max_new}"
        )
        targets = targets[:max_new]

    # Step 2: 회사개황 fetch
    logger.info(
        f"[tickers_kr] Step 2/3: DART /api/company.json × {len(targets)} 호출"
    )
    dart = get_dart_client() if targets else None
    payload: list[dict[str, Any]] = []
    fetched_meta = 0
    fetch_failed = 0

    for i, symbol in enumerate(targets, 1):
        corp_code, corp_name_cc = dart_listed[symbol]
        meta = _fetch_company_info(dart, corp_code) if dart is not None else None
        if meta is None:
            fetch_failed += 1
            # 실패 시에도 symbol+name+corp_code 만큼은 저장 (NULL 메타로)
            payload.append({
                "symbol": symbol,
                "name": corp_name_cc,
                "corp_code": corp_code,
                "corp_cls": None,
                "induty_code": None,
                "est_dt": None,
                "acc_mt": None,
            })
        else:
            fetched_meta += 1
            payload.append({
                "symbol": symbol,
                # dart_corp_codes 와 company.json 의 corp_name 이 다르면
                # company.json 쪽을 신뢰 (더 최신).
                "name": meta["corp_name_dart"] or corp_name_cc,
                "corp_code": corp_code,
                "corp_cls": meta["corp_cls"],
                "induty_code": meta["induty_code"],
                "est_dt": meta["est_dt"],
                "acc_mt": meta["acc_mt"],
            })

        if delay > 0:
            time.sleep(delay)
        if i % 100 == 0:
            logger.info(
                f"  진행: {i}/{len(targets)} "
                f"(fetched={fetched_meta}, failed={fetch_failed})"
            )

    # Step 3: upsert + delisted 마킹
    logger.info("[tickers_kr] Step 3/3: tickers upsert + delisted 마킹")
    inserted, updated, delisted_marked = _apply_changes(
        payload, missing_symbols, today,
    )

    result = {
        "fetched_meta": fetched_meta,
        "fetch_failed": fetch_failed,
        "inserted": inserted,
        "updated": updated,
        "delisted_marked": delisted_marked,
        "skipped_existing": max(
            0, len(dart_symbols) - len(targets) - len(missing_symbols)
        ),
    }
    logger.success(f"[tickers_kr] 완료: {result}")
    return result


# ---------------------------------------------------------------------------
# Upsert helper
# ---------------------------------------------------------------------------


def _apply_changes(
    payload: list[dict[str, Any]],
    missing_symbols: set[str],
    ref_date: date,
) -> tuple[int, int, int]:
    """tickers upsert + missing → delisted 마킹.

    Returns:
        (inserted, updated, delisted_marked)
    """
    if not payload and not missing_symbols:
        return 0, 0, 0

    inserted = updated = delisted_marked = 0
    with session_scope() as session:
        if payload:
            existing_set = {
                s for (s,) in session.execute(select(Ticker.symbol)).all()
            }

            # 키 셋 일관성 보장 (PG bulk INSERT 의 컬럼 정렬용)
            for p in payload:
                p.setdefault("corp_cls", None)
                p.setdefault("induty_code", None)
                p.setdefault("est_dt", None)
                p.setdefault("acc_mt", None)
                p["delisted"] = False
                p["delisted_date"] = None

            stmt = pg_insert(Ticker).values(payload)
            stmt = stmt.on_conflict_do_update(
                index_elements=["symbol"],
                set_={
                    "name": stmt.excluded.name,
                    "corp_code": stmt.excluded.corp_code,
                    "corp_cls": stmt.excluded.corp_cls,
                    "induty_code": stmt.excluded.induty_code,
                    "est_dt": stmt.excluded.est_dt,
                    "acc_mt": stmt.excluded.acc_mt,
                    "delisted": False,
                    "delisted_date": None,
                    "updated_at": func.now(),
                },
            )
            session.execute(stmt)

            payload_syms = {p["symbol"] for p in payload}
            inserted = len(payload_syms - existing_set)
            updated = len(payload_syms & existing_set)

        if missing_symbols:
            session.execute(
                update(Ticker)
                .where(
                    Ticker.symbol.in_(missing_symbols),
                    Ticker.delisted.is_(False),
                )
                .values(delisted=True, delisted_date=ref_date)
            )
            delisted_marked = len(missing_symbols)

    return inserted, updated, delisted_marked


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "DART corp_codes + /api/company.json 으로 tickers 테이블 갱신. "
            "DART_API_KEY 가 .env 에 필요."
        ),
    )
    parser.add_argument(
        "--force-all",
        action="store_true",
        help=(
            "DART 가 인지하는 모든 상장 종목의 메타를 재수집. "
            "부트스트랩 / 스키마 변경 후 1회 권장."
        ),
    )
    parser.add_argument(
        "--max-new",
        type=int,
        default=None,
        metavar="N",
        help="한 실행에서 처리할 신규 종목 최대치 (DART 호출 한도 분할용).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        metavar="SECONDS",
        help=f"DART 호출 간 sleep 초 (기본 {DEFAULT_REQUEST_DELAY}).",
    )
    args = parser.parse_args()

    refresh_tickers_from_dart(
        force_all=args.force_all,
        max_new=args.max_new,
        request_delay=args.delay,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
