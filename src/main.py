"""Daily 03:00 KST cron entry point — KIS 일봉 + DART 공시 수집.

매일 오전 3시에 실행되는 통합 일일 수집 파이프라인.

Steps:
    1. KIS 일봉 수집 (전일 OHLCV + 시총/PER/PBR/외국인 스냅샷)
       — 내부적으로 src.collectors.daily_kis.backfill_symbols 호출
    2. DART 공시 수집 (전일 공시된 정기보고서/주요사항/지분 등)
       — 내부적으로 src.collectors.dart_disclosures.collect_disclosures 호출

설계 원칙:
    - Step 1 실패가 Step 2를 막지 않는다 (둘 다 try/except 격리).
    - 종료 코드는 두 단계의 성공/실패를 비트로 표현:
        0  = 모두 성공
        1  = KIS 단계 실패
        2  = DART 단계 실패
        3  = 둘 다 실패
        4  = 사전 검증 실패 (.env 누락 등)
        130 = 사용자 중단 (Ctrl-C)
    - 휴장일/주말 처리는 각 collector 내부의 resume 로직(`skip_done=True`)에
      위임한다. 03:00 KST 시점에는 KRX 데이터가 아직 전일자라 "어제 = 직전
      거래일" 가정이 안전하다.

Usage:
    # 운영 — Windows 작업 스케줄러 / cron에서 매일 03:00 호출
    python -m src.main

    # 수동 — 특정 날짜로 재실행 (예: 어제 실패 복구)
    python -m src.main --date 2026-05-13

    # 수동 — 직전 며칠치 안전마진 (휴장일 끼면 늘리는 게 안전)
    python -m src.main --days 3

    # KIS 또는 DART 한쪽만 실행
    python -m src.main --only kis
    python -m src.main --only dart

    # snapshot 생략 (OHLCV만, KIS 호출 절반)
    python -m src.main --no-snapshot

스케줄러 등록 (Windows 작업 스케줄러):
    프로그램/스크립트: C:\\Users\\Playdata\\workspace\\stock_trader\\.venv\\Scripts\\python.exe
    인수:           -m src.main
    시작 위치:       C:\\Users\\Playdata\\workspace\\stock_trader
    트리거:          매일 03:00
"""
from __future__ import annotations

# .env는 모든 src.* import보다 먼저 로드되어야 한다
# (KRX_ID/KRX_PW, DART_API_KEY, KIS_* 등이 모듈 import 시점에 평가될 수 있음)
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import argparse  # noqa: E402
from datetime import date, datetime, timedelta  # noqa: E402
from typing import Literal  # noqa: E402

from src.collectors.daily_kis import backfill_symbols  # noqa: E402
from src.collectors.dart_corp_codes import collect_corp_codes  # noqa: E402
from src.collectors.dart_disclosures import (  # noqa: E402
    DEFAULT_KINDS,
    collect_disclosures,
)
from src.config import get_kis_settings  # noqa: E402
from src.utils.logger import logger  # noqa: E402

# Exit code bits (조합 가능)
EXIT_OK = 0
EXIT_KIS_FAILED = 1
EXIT_DART_FAILED = 2
EXIT_PRECHECK_FAILED = 4
EXIT_INTERRUPTED = 130


def _parse_date(s: str) -> date:
    """Accept YYYY-MM-DD or YYYYMMDD."""
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Invalid date {s!r}; use YYYY-MM-DD or YYYYMMDD."
    )


def _precheck_kis() -> bool:
    """KIS 자격증명이 .env에 채워져 있는지 확인."""
    settings = get_kis_settings()
    if not settings.kis_app_key or not settings.kis_app_secret:
        logger.error(
            "KIS_APP_KEY / KIS_APP_SECRET이 .env에 비어 있습니다. "
            "https://apiportal.koreainvestment.com 에서 발급 후 입력하세요."
        )
        return False
    return True


def _precheck_dart() -> bool:
    """DART_API_KEY가 .env에 있는지 확인."""
    import os
    if not os.getenv("DART_API_KEY"):
        logger.error(
            "DART_API_KEY가 .env에 비어 있습니다. "
            "https://opendart.fss.or.kr/ 에서 인증키를 발급받으세요."
        )
        return False
    return True


def run_kis_daily(
    end_date: date,
    days: int,
    fetch_snapshot: bool,
    skip_done: bool,
) -> bool:
    """KIS 일봉 수집 실행. 성공 시 True."""
    start_date = end_date - timedelta(days=max(0, days - 1))
    logger.info(
        f"▶ KIS daily: window={start_date} → {end_date}, "
        f"snapshot={fetch_snapshot}, skip_done={skip_done}"
    )
    try:
        backfill_symbols(
            symbols=None,
            start_date=start_date,
            end_date=end_date,
            skip_done=skip_done,
            fetch_snapshot=fetch_snapshot,
        )
        logger.success("✓ KIS daily 완료")
        return True
    except Exception as e:  # noqa: BLE001
        logger.exception(f"✗ KIS daily 실패: {e}")
        return False


def run_dart_daily(
    end_date: date,
    days: int,
    skip_done: bool,
) -> bool:
    """DART 공시 수집 실행. 성공 시 True.

    Step A: corp_codes 마스터를 stale 정책에 따라 갱신.
    Step B: 전일자(또는 days만큼 직전) 공시 수집.
    """
    logger.info(f"▶ DART daily: end={end_date}, days={days}, skip_done={skip_done}")
    try:
        # Step A: corp_codes (cache가 fresh면 자동으로 skip 됨)
        collect_corp_codes(force=False)

        # Step B: disclosures
        start_date = end_date - timedelta(days=max(0, days - 1))
        collect_disclosures(
            start_date=start_date,
            end_date=end_date,
            kinds=DEFAULT_KINDS,
            listed_only=True,
            skip_done=skip_done,
        )
        logger.success("✓ DART daily 완료")
        return True
    except Exception as e:  # noqa: BLE001
        logger.exception(f"✗ DART daily 실패: {e}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Daily 03:00 cron entry point — KIS 일봉 + DART 공시 수집.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        help=(
            "Target end date (YYYY-MM-DD). Default: 어제 (실행일 - 1). "
            "03:00 KST 실행 가정이므로 '오늘 새벽 = 어제 데이터' 가 자연스럽다."
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help=(
            "Backfill window size in calendar days ending at --date. "
            "Default: 1. 휴장일이 끼면 3~5로 늘려 안전마진 확보."
        ),
    )
    parser.add_argument(
        "--only",
        choices=("kis", "dart"),
        default=None,
        help="한쪽만 실행. 생략 시 두 단계 모두 실행.",
    )
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help="KIS inquire-price snapshot 생략 (시총/PER/PBR/외국인 제외, OHLCV만).",
    )
    parser.add_argument(
        "--no-skip-done",
        action="store_true",
        help="resume 무시하고 강제 재수집.",
    )
    args = parser.parse_args()

    # 기본 end_date: 어제 (실행 시점이 03:00 KST 새벽이므로)
    end_date: date = args.date or (date.today() - timedelta(days=1))
    skip_done = not args.no_skip_done
    fetch_snapshot = not args.no_snapshot

    logger.info("=" * 70)
    logger.info(f"daily cron 시작: end_date={end_date}, only={args.only or 'all'}")
    logger.info("=" * 70)

    exit_code = EXIT_OK
    targets: tuple[Literal["kis", "dart"], ...]
    if args.only == "kis":
        targets = ("kis",)
    elif args.only == "dart":
        targets = ("dart",)
    else:
        targets = ("kis", "dart")

    # --- KIS step ---
    if "kis" in targets:
        if not _precheck_kis():
            return EXIT_PRECHECK_FAILED
        try:
            ok = run_kis_daily(
                end_date=end_date,
                days=args.days,
                fetch_snapshot=fetch_snapshot,
                skip_done=skip_done,
            )
        except KeyboardInterrupt:
            logger.warning("KIS 단계에서 Ctrl-C로 중단되었습니다.")
            return EXIT_INTERRUPTED
        if not ok:
            exit_code |= EXIT_KIS_FAILED

    # --- DART step (KIS 실패와 무관하게 진행) ---
    if "dart" in targets:
        if not _precheck_dart():
            # KIS만 성공한 케이스라도 precheck 실패는 분리해서 표시
            return exit_code | EXIT_PRECHECK_FAILED
        try:
            ok = run_dart_daily(
                end_date=end_date,
                days=args.days,
                skip_done=skip_done,
            )
        except KeyboardInterrupt:
            logger.warning("DART 단계에서 Ctrl-C로 중단되었습니다.")
            return exit_code | EXIT_INTERRUPTED
        if not ok:
            exit_code |= EXIT_DART_FAILED

    logger.info("=" * 70)
    if exit_code == EXIT_OK:
        logger.success("daily cron 완료 — 모든 단계 성공")
    else:
        logger.warning(f"daily cron 완료 — exit_code={exit_code} (실패 단계 있음)")
    logger.info("=" * 70)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
