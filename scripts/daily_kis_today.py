"""Daily incremental collection — KIS API.

오늘 (또는 특정 단일 날짜) 하루치 일봉을 전종목에 대해 수집한다.
운영 중 매일 장마감 후 또는 다음날 장 시작 전에 cron/스케줄러에서 호출하는
용도로 만들어졌다. KIS의 일봉 backfill collector(daily_kis)와 동일한 로직을
재사용하되, 윈도우만 "오늘 하루"로 좁힌다.

Usage:
    # 오늘자 (실행일 기준)
    python -m scripts.daily_kis_today

    # 특정 단일 날짜
    python -m scripts.daily_kis_today --date 2026-05-08

    # 짧은 backfill window (현재일 기준 N일)
    # 휴장일이 끼면 일봉이 안 잡힐 수 있으므로 며칠치 안전마진을 두고 싶을 때
    python -m scripts.daily_kis_today --days 3

    # snapshot (시총/PER/PBR/외국인) 생략 — OHLCV만, 더 빠름
    python -m scripts.daily_kis_today --no-snapshot

    # 강제 재수집 (resume 무시)
    python -m scripts.daily_kis_today --no-skip-done

Why a separate script vs `python -m src.collectors.daily_kis`:
    daily_kis 모듈을 직접 실행하면 cfg.collection.daily.backfill_days(기본 400일)
    가 적용되어 매일 돌리기엔 과하다. 이 스크립트는 "장마감 후 일일 증분"
    유스케이스를 명확히 표현하고, --date / --days 같은 운영 플래그를 가까이
    둔다.
"""
from __future__ import annotations

# .env를 import 전에 로드 (다른 collector script들과 동일 패턴)
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import argparse  # noqa: E402
from datetime import date, datetime, timedelta  # noqa: E402

from src.collectors.daily_kis import backfill_symbols  # noqa: E402
from src.config import get_kis_settings  # noqa: E402
from src.utils.logger import logger  # noqa: E402


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run daily_kis collector for a single recent day.",
    )
    parser.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        help="Target date (YYYY-MM-DD). Default: today.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help=(
            "Backfill window size in calendar days ending at --date. "
            "Default: 1 (just the target day). Bump to 3-5 if you want to "
            "fill in any recent days that prior runs missed."
        ),
    )
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help="Skip inquire-price snapshot (only OHLCV). Halves call budget.",
    )
    parser.add_argument(
        "--no-skip-done",
        action="store_true",
        help="Force re-collection even for symbols already logged success.",
    )
    args = parser.parse_args()

    settings = get_kis_settings()
    if not settings.kis_app_key or not settings.kis_app_secret:
        logger.error(
            "KIS_APP_KEY / KIS_APP_SECRET are empty in .env. "
            "Issue a key at https://apiportal.koreainvestment.com first."
        )
        return 1

    end_date = args.date or date.today()
    start_date = end_date - timedelta(days=max(0, args.days - 1))

    logger.info(
        f"daily_kis incremental: mode={settings.kis_mode}, "
        f"window={start_date} → {end_date}, snapshot={not args.no_snapshot}"
    )

    try:
        backfill_symbols(
            symbols=None,                       # all active tickers
            start_date=start_date,
            end_date=end_date,
            skip_done=not args.no_skip_done,
            fetch_snapshot=not args.no_snapshot,
        )
    except KeyboardInterrupt:
        logger.warning("Aborted by user (Ctrl-C). Re-run to resume.")
        return 130
    except Exception as e:  # noqa: BLE001
        logger.exception(f"daily_kis incremental run failed: {e}")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
