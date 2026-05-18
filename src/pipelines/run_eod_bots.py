"""Daily cron entry point — run all RUNNING bots for a decision date.

Usage:
    # 가장 최근 영업일 기준으로 모든 RUNNING 봇 tick:
    python -m src.pipelines.run_eod_bots

    # 특정 날짜:
    python -m src.pipelines.run_eod_bots --date 2026-05-15

    # 특정 봇 하나만 (디버깅):
    python -m src.pipelines.run_eod_bots --bot-id 12345678-1234-...

Place in the daily cron AFTER factor production finishes (factor_signals
must be up-to-date for `decision_date`). The bot runner reads from the
factor_signals cache; it does NOT recompute factors.

Exit codes:
    0 - all bots either SUCCESS or SKIPPED (no failures)
    1 - at least one bot returned FAILED
    2 - argument / setup error
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# Load .env BEFORE any src.* import that reads credentials.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)

from src.trading.bot import run_all_running_bots, run_bot_daily  # noqa: E402
from src.utils.calendar import latest_business_day  # noqa: E402
from src.utils.logger import logger  # noqa: E402


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Run EOD bot daily ticks.")
    ap.add_argument(
        "--date",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="Decision date (YYYY-MM-DD). Default: latest_business_day().",
    )
    ap.add_argument(
        "--bot-id",
        type=str,
        default=None,
        help="Run only this bot. UUID. Useful for debugging.",
    )
    return ap.parse_args()


def main() -> int:
    args = _parse_args()
    decision = args.date or latest_business_day()

    if args.bot_id:
        try:
            bid = uuid.UUID(args.bot_id)
        except ValueError:
            logger.error(f"--bot-id {args.bot_id!r} is not a valid UUID")
            return 2
        logger.info(f"[run_eod_bots] single bot {bid} on {decision}")
        result = run_bot_daily(bid, decision)
        _print_result(result)
        return 0 if result["status"] != "FAILED" else 1

    logger.info(f"[run_eod_bots] all RUNNING bots on {decision}")
    results = run_all_running_bots(decision)
    if not results:
        logger.info("[run_eod_bots] no RUNNING bots — nothing to do")
        return 0

    n_success = sum(1 for r in results if r["status"] == "SUCCESS")
    n_skipped = sum(1 for r in results if r["status"] == "SKIPPED")
    n_failed = sum(1 for r in results if r["status"] == "FAILED")

    logger.info(
        f"[run_eod_bots] {decision}: "
        f"SUCCESS={n_success}, SKIPPED={n_skipped}, FAILED={n_failed} "
        f"(of {len(results)} total)"
    )
    for r in results:
        _print_result(r)

    return 1 if n_failed > 0 else 0


def _print_result(r: dict) -> None:
    """Log a one-liner per bot result."""
    bid = r.get("bot_id")
    status = r.get("status")
    if status == "SUCCESS":
        logger.success(
            f"  bot={bid} {status} buys={r.get('n_buys')} "
            f"sells={r.get('n_sells')} total={r.get('total_value')}"
        )
    elif status == "SKIPPED":
        logger.info(f"  bot={bid} {status} reason={r.get('reason')}")
    else:  # FAILED
        logger.error(
            f"  bot={bid} {status} reason={r.get('reason')} "
            f"error={r.get('error')}"
        )


if __name__ == "__main__":
    sys.exit(main())
