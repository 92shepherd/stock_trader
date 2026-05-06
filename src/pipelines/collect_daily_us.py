"""CLI entry point for US daily price collection (yfinance).

This is the US counterpart to `collect_daily.py` (Korea / pykrx) and
`collect_daily_fdr.py` (Korea / FDR). It does two things:

  Step 1: Refresh the `tickers_us` master from NASDAQ Trader's
          daily-updated public files. Idempotent.
  Step 2: yfinance batch-download daily OHLCV for the active universe
          (~6,000 symbols) over the requested date range, with resume
          support via collection_log.

A fresh full-universe backfill takes 5–15 min depending on network and
Yahoo's mood. Resumed runs (skip_done=True, default) are essentially
free when there's nothing new.

Usage:
    # Refresh master + backfill 400 calendar days for the full universe.
    # Re-running picks up where it left off.
    python -m src.pipelines.collect_daily_us

    # Specific date range
    python -m src.pipelines.collect_daily_us --start 2024-01-01 --end 2024-12-31

    # Skip the master refresh (use existing rows in tickers_us)
    python -m src.pipelines.collect_daily_us --days 400 --skip-tickers

    # Force re-collection (ignore prior success log entries)
    python -m src.pipelines.collect_daily_us --days 400 --no-skip-done

    # Specific symbols only (skips the master refresh and full universe)
    python -m src.pipelines.collect_daily_us --symbols AAPL MSFT NVDA

    # Common stock only (no ETFs/ADRs/preferreds)
    python -m src.pipelines.collect_daily_us --security-types COMMON

    # NASDAQ + NYSE only
    python -m src.pipelines.collect_daily_us --exchanges NASDAQ NYSE
"""
from __future__ import annotations

# Load .env early — keeps DB creds available for downstream modules.
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import argparse
from datetime import date

from src.collectors.daily_us_yf import (
    backfill_active_universe,
    backfill_symbols,
)
from src.collectors.tickers_us import collect_us_tickers
from src.utils.logger import logger


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="US daily price collection pipeline (yfinance)"
    )
    parser.add_argument("--start", type=_parse_date, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=_parse_date, help="End date (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, help="Backfill N days from end_date")
    parser.add_argument(
        "--symbols", nargs="+", metavar="SYMBOL",
        help="Limit to specific tickers (e.g., AAPL MSFT NVDA). "
             "Skips ticker-master refresh and full-universe selection.",
    )
    parser.add_argument(
        "--exchanges", nargs="+", default=None, metavar="EXCHANGE",
        help="Exchanges to include for full-universe backfill "
             "(e.g., NASDAQ NYSE AMEX). Default: all.",
    )
    parser.add_argument(
        "--security-types", nargs="+", default=None, metavar="TYPE",
        help="Security types to include "
             "(e.g., COMMON ETF ADR PREFERRED). Default: all non-test.",
    )
    parser.add_argument(
        "--skip-tickers", action="store_true",
        help="Skip ticker master refresh (use existing rows in tickers_us).",
    )
    parser.add_argument(
        "--no-skip-done", action="store_true",
        help="Re-collect symbols even if already marked success in "
             "collection_log for this end_date.",
    )
    args = parser.parse_args()

    # Pinned-symbols path: skip the master refresh; user is being specific.
    if args.symbols:
        logger.info(
            f"=== US backfill: {len(args.symbols)} pinned symbol(s) ==="
        )
        backfill_symbols(
            symbols=args.symbols,
            start_date=args.start,
            end_date=args.end,
            days=args.days,
            skip_done=not args.no_skip_done,
        )
        return

    # Step 1: refresh ticker master (NASDAQ Trader files)
    if not args.skip_tickers:
        logger.info("=== Step 1: Refresh US ticker master ===")
        collect_us_tickers()
    else:
        logger.info("=== Step 1: Skipped ticker master refresh ===")

    # Step 2: full active universe daily backfill
    logger.info("=== Step 2: Collect US daily prices (yfinance) ===")
    backfill_active_universe(
        start_date=args.start,
        end_date=args.end,
        days=args.days,
        exchanges=args.exchanges,
        security_types=args.security_types,
        skip_done=not args.no_skip_done,
    )


if __name__ == "__main__":
    main()
