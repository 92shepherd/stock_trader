"""CLI entry point for FnGuide consensus collection.

Usage:
    # 기본: 오늘 날짜로 전 종목 스냅샷, ticker 마스터 새로고침 포함
    python -m src.pipelines.collect_consensus_fnguide

    # 특정 날짜로 스냅샷 (재실행 / 어제 backfill)
    python -m src.pipelines.collect_consensus_fnguide --as-of 2026-05-15

    # 특정 종목만
    python -m src.pipelines.collect_consensus_fnguide --symbols 005930 000660

    # ticker 마스터 새로고침 건너뜀 (이미 오늘 돌렸을 때)
    python -m src.pipelines.collect_consensus_fnguide --skip-tickers

    # 강제 재수집 (이미 success 로그가 있어도 다시)
    python -m src.pipelines.collect_consensus_fnguide --no-skip-done

Pre-requisites:
    .env 에 FNGUIDE_CONSENT_ACK=1 가 설정되어 있어야 한다. 없으면
    collector 모듈 진입 직후 RuntimeError 로 거부됨.
"""
from __future__ import annotations

# Load .env early so any downstream module that reads os.environ
# (DB creds, FNGUIDE_CONSENT_ACK 등) sees the right values.
# override=True 은 의도적: 세션의 (혹은 빈 값의) 기존 환경변수보다
# .env 파일을 우선. 예: FNGUIDE_CONSENT_ACK="" 가 먼저 설정되어 있으면
# load_dotenv() 기본값으로는 덮어쓰지 않아 .env 의 =1 이 무시됨.
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)

import argparse
from datetime import date

from src.collectors.consensus_fnguide import (
    backfill_active_universe,
    backfill_symbols,
)
from src.collectors.tickers import collect_tickers_fdr
from src.utils.logger import logger


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="FnGuide consensus snapshot pipeline (개인용 비공개)"
    )
    parser.add_argument(
        "--as-of", type=_parse_date, dest="as_of",
        help="스냅샷 날짜 (YYYY-MM-DD). 기본값 = 오늘.",
    )
    parser.add_argument(
        "--symbols", nargs="+", metavar="SYMBOL",
        help="특정 6자리 종목코드만 (예: 005930 000660). "
             "지정 시 ticker 마스터 refresh 와 active universe 선택을 건너뜀.",
    )
    parser.add_argument(
        "--markets", nargs="+", default=None, metavar="MARKET",
        help="전 종목 모드에서 대상 시장 (기본: config 설정). 예: KOSPI KOSDAQ",
    )
    parser.add_argument(
        "--skip-tickers", action="store_true",
        help="Ticker 마스터 refresh 를 건너뜀 (이미 오늘 돌렸을 때).",
    )
    parser.add_argument(
        "--no-skip-done", action="store_true",
        help="이미 success 로 기록된 종목도 재수집.",
    )
    parser.add_argument(
        "--delay", type=float, default=None, metavar="SECONDS",
        help="요청 간 sleep 초 (기본: max(config.daily.request_delay, 1.0)).",
    )
    args = parser.parse_args()

    # Pinned-symbols path: ticker 마스터 refresh 생략
    if args.symbols:
        logger.info(
            f"=== FnGuide consensus: {len(args.symbols)} pinned symbol(s) ==="
        )
        backfill_symbols(
            symbols=args.symbols,
            as_of_date=args.as_of,
            skip_done=not args.no_skip_done,
            request_delay=args.delay,
        )
        return

    # Step 1: ticker 마스터 refresh
    if not args.skip_tickers:
        logger.info("=== Step 1: Refresh ticker master ===")
        collect_tickers_fdr(desc=True)
    else:
        logger.info("=== Step 1: Skipped ticker master refresh ===")

    # Step 2: 전 종목 컨센서스 스냅샷
    logger.info("=== Step 2: Collect FnGuide consensus snapshots ===")
    backfill_active_universe(
        as_of_date=args.as_of,
        markets=args.markets,
        skip_done=not args.no_skip_done,
        request_delay=args.delay,
    )


if __name__ == "__main__":
    main()
