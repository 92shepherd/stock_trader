"""CLI entry point for FDR-based daily price collection.

This is the FDR counterpart to `collect_daily.py` (which uses pykrx).
Use this when KRX credentials are unavailable or you simply prefer the
FDR data path. It fetches per-symbol, so a fresh full-universe backfill
takes 1–3 hours; the resume flag (`skip_done`, on by default) makes
interrupted runs cheap to restart.

Usage:
    # Backfill 400 calendar days (default) for the full active universe.
    # Re-running picks up where it left off.
    python -m src.pipelines.collect_daily_fdr

    # Specific date range.
    python -m src.pipelines.collect_daily_fdr --start 2024-04-21 --end 2025-04-21

    # Skip the ticker-master refresh (already done today).
    python -m src.pipelines.collect_daily_fdr --days 400 --skip-tickers

    # Force re-collection (ignore prior success log entries).
    python -m src.pipelines.collect_daily_fdr --days 400 --no-skip-done

    # Collect only specific symbols.
    python -m src.pipelines.collect_daily_fdr --symbols 005930 000660 035420
"""
from __future__ import annotations

# Load .env early so any downstream module that reads os.environ
# (DB creds, optional credentials, etc.) sees the right values.
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import argparse
from datetime import date

from src.collectors.daily_fdr import (
    backfill_active_universe,
    backfill_symbols,
)
from src.collectors.tickers import collect_tickers_fdr
from src.utils.logger import logger


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Daily price collection pipeline (FinanceDataReader)"
    )
    parser.add_argument("--start", type=_parse_date, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=_parse_date, help="End date (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, help="Backfill N days from end_date")
    parser.add_argument(
        "--symbols", nargs="+", metavar="SYMBOL",
        help="Limit to specific 6-digit symbols (e.g., 005930 000660). "
             "Skips ticker-master refresh and full-universe selection.",
    )
    parser.add_argument(
        "--markets", nargs="+", default=None, metavar="MARKET",
        help="Markets to include for full-universe backfill "
             "(default: from config/settings.yaml). e.g., KOSPI KOSDAQ",
    )
    parser.add_argument(
        "--skip-tickers", action="store_true",
        help="Skip ticker master refresh (use existing rows in `tickers`).",
    )
    parser.add_argument(
        "--no-skip-done", action="store_true",
        help="Re-collect symbols even if already marked success in "
             "collection_log for this end_date.",
    )
    parser.add_argument(
        "--no-desc", action="store_true",
        help="Use the cheaper StockListing(KOSPI/KOSDAQ) variant for the "
             "ticker refresh — no sector/industry/listing_date columns.",
    )
    args = parser.parse_args()

    # When user pinned specific symbols, skip the master refresh: it's
    # neither needed nor what they asked for.
    if args.symbols:
        logger.info(
            f"=== FDR backfill: {len(args.symbols)} pinned symbol(s) ==="
        )
        backfill_symbols(
            symbols=args.symbols,
            start_date=args.start,
            end_date=args.end,
            days=args.days,
            skip_done=not args.no_skip_done,
        )
        return

    # Step 1: refresh ticker master (FDR-based; gives sector/industry too)
    if not args.skip_tickers:
        logger.info("=== Step 1: Refresh ticker master (FDR) ===")
        collect_tickers_fdr(desc=not args.no_desc)
    else:
        logger.info("=== Step 1: Skipped ticker master refresh ===")

    # Step 2: full active universe daily backfill
    logger.info("=== Step 2: Collect daily prices (FDR, full universe) ===")
    backfill_active_universe(
        start_date=args.start,
        end_date=args.end,
        days=args.days,
        markets=args.markets,
        skip_done=not args.no_skip_done,
    )


if __name__ == "__main__":
    main()
