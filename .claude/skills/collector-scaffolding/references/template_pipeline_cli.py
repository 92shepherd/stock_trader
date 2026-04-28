"""CLI entry point for <COLLECTOR_PURPOSE>.

This is the <COLLECTOR_NAME> counterpart to the other collect_* pipelines.
<Add a paragraph explaining when to use this vs alternatives, performance
expectations, etc. Mirror collect_daily_fdr.py's docstring style.>

Usage:
    # Default backfill — picks up where it left off if interrupted
    python -m src.pipelines.collect_<n>

    # Specific date range
    python -m src.pipelines.collect_<n> --start 2024-01-01 --end 2025-12-31

    # Skip ticker-master refresh (already done today)
    python -m src.pipelines.collect_<n> --days 400 --skip-tickers

    # Force re-collection (ignore prior success log entries)
    python -m src.pipelines.collect_<n> --days 400 --no-skip-done

    # Specific symbols only
    python -m src.pipelines.collect_<n> --symbols 005930 000660 035420
"""
from __future__ import annotations

# Load .env early so any downstream module that reads os.environ
# (DB creds, KIS keys, DART API keys, etc.) sees the right values.
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import argparse
from datetime import date

from src.collectors.<n> import (
    backfill_active_universe,
    backfill_symbols,
)
from src.collectors.tickers import collect_tickers_fdr  # or collect_tickers
from src.utils.logger import logger


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="<COLLECTOR_PURPOSE> pipeline"
    )
    parser.add_argument(
        "--start", type=_parse_date, help="Start date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end", type=_parse_date, help="End date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--days", type=int, help="Backfill N days from end_date"
    )
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
        help="Re-collect even if already marked success in collection_log.",
    )
    args = parser.parse_args()

    # Pinned-symbols path: skip the master refresh; the user is being specific.
    if args.symbols:
        logger.info(
            f"=== <COLLECTOR>: {len(args.symbols)} pinned symbol(s) ==="
        )
        backfill_symbols(
            symbols=args.symbols,
            start_date=args.start,
            end_date=args.end,
            days=args.days,
            skip_done=not args.no_skip_done,
        )
        return

    # Step 1: refresh ticker master (skip if --skip-tickers)
    if not args.skip_tickers:
        logger.info("=== Step 1: Refresh ticker master ===")
        collect_tickers_fdr(desc=True)
    else:
        logger.info("=== Step 1: Skipped ticker master refresh ===")

    # Step 2: full active universe backfill
    logger.info("=== Step 2: Collect <data> ===")
    backfill_active_universe(
        start_date=args.start,
        end_date=args.end,
        days=args.days,
        markets=args.markets,
        skip_done=not args.no_skip_done,
    )


if __name__ == "__main__":
    main()
