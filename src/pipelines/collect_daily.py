"""CLI entry point for daily price collection.

Usage:
    # Backfill 400 calendar days (default)
    python -m src.pipelines.collect_daily

    # Specific range
    python -m src.pipelines.collect_daily --start 2024-04-21 --end 2025-04-21

    # Only refresh today's data (for daily cron)
    python -m src.pipelines.collect_daily --today
"""
from __future__ import annotations

import argparse
from datetime import date

from src.collectors.daily_pykrx import backfill, collect_daily_for_date
from src.collectors.tickers import collect_tickers
from src.db.repositories import log_collection
from src.utils.logger import logger


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily price collection pipeline")
    parser.add_argument("--start", type=_parse_date, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=_parse_date, help="End date (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, help="Backfill N days from end_date")
    parser.add_argument(
        "--today", action="store_true", help="Collect only today's data",
    )
    parser.add_argument(
        "--skip-tickers", action="store_true",
        help="Skip ticker master refresh",
    )
    args = parser.parse_args()

    # Step 1: refresh ticker master
    if not args.skip_tickers:
        logger.info("=== Step 1: Refresh ticker master ===")
        collect_tickers()

    # Step 2: collect daily prices
    logger.info("=== Step 2: Collect daily prices ===")
    if args.today:
        today = date.today()
        try:
            rows = collect_daily_for_date(today)
            log_collection("daily_pykrx", today, "success", rows_inserted=rows)
            logger.success(f"Today ({today}): {rows} rows")
        except Exception as e:
            log_collection(
                "daily_pykrx", today, "failed", error_message=str(e)[:500]
            )
            raise
    else:
        backfill(start_date=args.start, end_date=args.end, days=args.days)


if __name__ == "__main__":
    main()
