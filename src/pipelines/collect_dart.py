"""CLI entry point for DART data collection (Phase 1 + Phase 2).

Pipeline steps (each can be toggled on/off independently):
  Step 1 (corp_codes):    Refresh dart_corp_codes (skipped if fresh)
  Step 2 (disclosures):   Collect dart_disclosures for a date range
  Step 3 (financials):    Collect dart_financials for a year/quarter range
  Step 4 (indicators):    Collect dart_indicators for the same range

Default behavior (Phase 1 cron mode):
  Steps 1 + 2 only — yesterday's major-event disclosures.
  Phase 2 steps must be enabled with --financials / --indicators.

Usage:
    # Phase 1 — daily disclosure collection (cron-friendly)
    python -m src.pipelines.collect_dart

    # Phase 2 — backfill financials for a single quarter, capped at 5,000 calls
    python -m src.pipelines.collect_dart \\
        --financials \\
        --year 2024 --reprt 11014 --fs-divs CFS \\
        --max-calls 5000 \\
        --skip-disclosures

    # Phase 2 — full 2020+ backfill split across many runs
    # Run this once per day; resume picks up automatically
    python -m src.pipelines.collect_dart \\
        --financials --indicators \\
        --start-year 2020 \\
        --max-calls 9000 \\
        --skip-disclosures

    # Phase 2 — collect just the latest quarter for both
    python -m src.pipelines.collect_dart \\
        --financials --indicators \\
        --year 2025 --reprt 11013

    # Re-collect even if data already present
    python -m src.pipelines.collect_dart --financials --year 2024 --no-skip-done

    # Skip the corp_codes step (use existing cache)
    python -m src.pipelines.collect_dart --skip-corp-codes
"""
from __future__ import annotations

# Load .env early so DART_API_KEY is available downstream
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import argparse
from datetime import date

from src.collectors.dart_corp_codes import collect_corp_codes
from src.collectors.dart_disclosures import (
    ALL_KINDS,
    DEFAULT_KINDS,
    collect_disclosures,
)
from src.collectors.dart_financials import (
    DEFAULT_FS_DIVS,
    DEFAULT_START_YEAR,
    REPRT_CODES,
    collect_financials,
)
from src.collectors.dart_indicators import (
    IDX_CL_CODES,
    collect_indicators,
)
from src.utils.logger import logger


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DART data collection pipeline (Phase 1 + Phase 2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ---- Step toggles ----
    parser.add_argument(
        "--skip-corp-codes", action="store_true",
        help="Skip Step 1 — dart_corp_codes refresh",
    )
    parser.add_argument(
        "--force-corp-codes", action="store_true",
        help="Force corp_codes refresh even if cache is fresh",
    )
    parser.add_argument(
        "--skip-disclosures", action="store_true",
        help="Skip Step 2 — dart_disclosures collection",
    )
    parser.add_argument(
        "--financials", action="store_true",
        help="ENABLE Step 3 — dart_financials collection (Phase 2)",
    )
    parser.add_argument(
        "--indicators", action="store_true",
        help="ENABLE Step 4 — dart_indicators collection (Phase 2)",
    )

    # ---- Disclosure (Phase 1) options ----
    grp_disc = parser.add_argument_group("disclosure options (Phase 1)")
    grp_disc.add_argument("--start", type=_parse_date, help="Start date (YYYY-MM-DD)")
    grp_disc.add_argument("--end", type=_parse_date, help="End date (YYYY-MM-DD)")
    grp_disc.add_argument(
        "--days", type=int,
        help="Backfill N days from end_date (default: 1)",
    )
    kind_group = grp_disc.add_mutually_exclusive_group()
    kind_group.add_argument(
        "--kinds", nargs="+", metavar="K",
        help=f"Disclosure kinds (default: {list(DEFAULT_KINDS)}). "
             f"Valid: {list(ALL_KINDS)} "
             "(A=정기, B=주요사항, C=발행, D=지분, E=기타, F=감사, "
             "G=펀드, H=자산유동화, I=거래소, J=공정위)",
    )
    kind_group.add_argument(
        "--kinds-all", action="store_true",
        help="Collect ALL disclosure kinds",
    )
    grp_disc.add_argument(
        "--include-non-listed", action="store_true",
        help="Also collect disclosures from non-listed companies",
    )

    # ---- Financial / indicator (Phase 2) options ----
    grp_fin = parser.add_argument_group("financials/indicators options (Phase 2)")
    grp_fin.add_argument(
        "--year", type=int,
        help="Single fiscal year (e.g. 2024). If set, --start-year/--end-year ignored.",
    )
    grp_fin.add_argument(
        "--start-year", type=int, default=DEFAULT_START_YEAR,
        help=f"Backfill start year (default: {DEFAULT_START_YEAR})",
    )
    grp_fin.add_argument(
        "--end-year", type=int,
        help="Backfill end year (default: current year)",
    )
    grp_fin.add_argument(
        "--reprt", nargs="+", metavar="CODE",
        help=f"Report codes to collect: {list(REPRT_CODES.keys())} "
             "(11013=Q1, 11012=H1, 11014=Q3, 11011=FY). Default: all 4.",
    )
    grp_fin.add_argument(
        "--fs-divs", nargs="+", metavar="DIV",
        choices=("CFS", "OFS"),
        help=f"fs_div values to collect (default: {list(DEFAULT_FS_DIVS)})",
    )
    grp_fin.add_argument(
        "--idx-cl", nargs="+", metavar="CODE",
        choices=tuple(IDX_CL_CODES.keys()),
        help=f"Indicator class codes (default: all 4: {list(IDX_CL_CODES.keys())}). "
             "Only applies to --indicators step.",
    )
    grp_fin.add_argument(
        "--max-calls", type=int,
        help="Hard cap on DART API calls per Phase 2 step. Useful for "
             "splitting backfills across days (DART limit: 10,000/day).",
    )

    # ---- Resume ----
    parser.add_argument(
        "--no-skip-done", action="store_true",
        help="Re-collect even if already marked success / data present",
    )

    args = parser.parse_args()

    # ----- Resolve disclosure args -----
    if args.kinds_all:
        kinds = ALL_KINDS
    elif args.kinds:
        kinds = tuple(args.kinds)
    else:
        kinds = DEFAULT_KINDS
    if not (args.start or args.end or args.days):
        args.days = 1

    # ----- Resolve financials/indicators year range -----
    if args.year:
        fin_start_year = fin_end_year = args.year
    else:
        fin_start_year = args.start_year
        fin_end_year = args.end_year or date.today().year

    fin_reprt = tuple(args.reprt) if args.reprt else tuple(REPRT_CODES.keys())
    fin_fs_divs = tuple(args.fs_divs) if args.fs_divs else DEFAULT_FS_DIVS
    fin_idx_cl = tuple(args.idx_cl) if args.idx_cl else tuple(IDX_CL_CODES.keys())

    # ===== Step 1: corp_codes =====
    if not args.skip_corp_codes:
        logger.info("=== Step 1: Refresh DART corp_codes ===")
        collect_corp_codes(force=args.force_corp_codes)
    else:
        logger.info("=== Step 1: Skipped corp_codes refresh ===")

    # ===== Step 2: disclosures =====
    if not args.skip_disclosures:
        logger.info("=== Step 2: Collect DART disclosures ===")
        collect_disclosures(
            start_date=args.start,
            end_date=args.end,
            days=args.days,
            kinds=kinds,
            listed_only=not args.include_non_listed,
            skip_done=not args.no_skip_done,
        )
    else:
        logger.info("=== Step 2: Skipped disclosures ===")

    # ===== Step 3: financials (Phase 2) =====
    if args.financials:
        logger.info(
            f"=== Step 3: Collect DART financials "
            f"({fin_start_year}-{fin_end_year}, "
            f"reprt={list(fin_reprt)}, fs_div={list(fin_fs_divs)}) ==="
        )
        result = collect_financials(
            start_year=fin_start_year,
            end_year=fin_end_year,
            reprt_codes=fin_reprt,
            fs_divs=fin_fs_divs,
            skip_done=not args.no_skip_done,
            max_calls=args.max_calls,
        )
        logger.info(f"  financials summary: {result}")

    # ===== Step 4: indicators (Phase 2) =====
    if args.indicators:
        logger.info(
            f"=== Step 4: Collect DART indicators "
            f"({fin_start_year}-{fin_end_year}, "
            f"reprt={list(fin_reprt)}, fs_div={list(fin_fs_divs)}) ==="
        )
        result = collect_indicators(
            start_year=fin_start_year,
            end_year=fin_end_year,
            reprt_codes=fin_reprt,
            fs_divs=fin_fs_divs,
            idx_cl_codes=fin_idx_cl,
            skip_done=not args.no_skip_done,
            max_calls=args.max_calls,
        )
        logger.info(f"  indicators summary: {result}")


if __name__ == "__main__":
    main()
