"""Smoke test for KIS daily-price collector (src/collectors/daily_kis.py).

Usage:
    # 기본: 삼성전자 + SK하이닉스 30일치 (snapshot 포함)
    python -m scripts.test_daily_kis

    # 특정 종목, 기간 지정
    python -m scripts.test_daily_kis --symbols 005930 035720 --days 60

    # snapshot 생략 (OHLCV만, 더 빠름)
    python -m scripts.test_daily_kis --no-snapshot

    # 강제 재수집 (skip_done 무시)
    python -m scripts.test_daily_kis --no-skip-done

What this verifies:
    1. KIS auth + quotations endpoints reachable.
    2. daily_prices and daily_prices_raw both get populated.
    3. inquire-price snapshot merges market_cap/per/pbr/foreign_net
       onto the end_date row of daily_prices.
    4. collection_log gets a 'success' row per symbol at target_date=end.

This script does NOT touch the full universe — pass --all if you want
that (or just call `python -m src.collectors.daily_kis` directly).
"""
from __future__ import annotations

# Load .env BEFORE importing modules that read os.environ at import time.
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import argparse  # noqa: E402
from datetime import date, timedelta  # noqa: E402

import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from src.collectors.daily_kis import COLLECTOR_NAME, backfill_symbols  # noqa: E402
from src.config import get_kis_settings  # noqa: E402
from src.db.connection import get_engine  # noqa: E402
from src.utils.logger import logger  # noqa: E402


_DEFAULT_SYMBOLS = ["005930", "000660"]  # 삼성전자, SK하이닉스


def _check_settings() -> bool:
    settings = get_kis_settings()
    missing = []
    if not settings.kis_app_key:
        missing.append("KIS_APP_KEY")
    if not settings.kis_app_secret:
        missing.append("KIS_APP_SECRET")
    if missing:
        logger.error(
            "KIS credentials missing in .env: " + ", ".join(missing)
        )
        return False
    logger.info(
        f"Mode: {settings.kis_mode}  |  app_key: "
        f"{settings.kis_app_key[:6]}…{settings.kis_app_key[-4:]}"
    )
    return True


def _show_results(symbols: list[str], end_date: date) -> None:
    """Print sample rows from daily_prices, daily_prices_raw, collection_log."""
    sql_adj = text(
        "SELECT symbol, date, open, close, volume, market_cap, per, pbr, foreign_net "
        "FROM daily_prices "
        "WHERE symbol = ANY(:syms) "
        "ORDER BY symbol, date DESC LIMIT 10"
    )
    sql_raw = text(
        "SELECT symbol, date, open, close, volume "
        "FROM daily_prices_raw "
        "WHERE symbol = ANY(:syms) "
        "ORDER BY symbol, date DESC LIMIT 10"
    )
    sql_log = text(
        "SELECT symbol, target_date, status, rows_inserted, duration_ms "
        "FROM collection_log "
        "WHERE collector = :col AND target_date = :end_date "
        "  AND symbol = ANY(:syms) "
        "ORDER BY symbol, id DESC"
    )

    with get_engine().connect() as conn:
        df_adj = pd.read_sql(sql_adj, conn, params={"syms": symbols})
        df_raw = pd.read_sql(sql_raw, conn, params={"syms": symbols})
        df_log = pd.read_sql(
            sql_log, conn,
            params={"col": COLLECTOR_NAME, "end_date": end_date, "syms": symbols},
        )

    pd.set_option("display.max_rows", None)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)

    logger.info("\n[daily_prices (수정주가) — 최근 10건]")
    logger.info("\n" + (df_adj.to_string(index=False) if not df_adj.empty else "(empty)"))

    logger.info("\n[daily_prices_raw (원주가) — 최근 10건]")
    logger.info("\n" + (df_raw.to_string(index=False) if not df_raw.empty else "(empty)"))

    logger.info("\n[collection_log — 이번 실행 결과]")
    logger.info("\n" + (df_log.to_string(index=False) if not df_log.empty else "(empty)"))


def main() -> int:
    parser = argparse.ArgumentParser(description="KIS daily collector smoke test")
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=_DEFAULT_SYMBOLS,
        help=f"Symbols to test (default: {' '.join(_DEFAULT_SYMBOLS)})",
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Backfill window in calendar days (default: 30).",
    )
    parser.add_argument(
        "--no-snapshot", action="store_true",
        help="Skip inquire-price snapshot (only OHLCV).",
    )
    parser.add_argument(
        "--no-skip-done", action="store_true",
        help="Force re-collection (ignore prior success log).",
    )
    args = parser.parse_args()

    if not _check_settings():
        return 1

    end_date = date.today()
    start_date = end_date - timedelta(days=args.days)

    logger.info(
        f"=== KIS daily smoke test: symbols={args.symbols}, "
        f"{start_date} → {end_date} ==="
    )

    try:
        backfill_symbols(
            symbols=args.symbols,
            start_date=start_date,
            end_date=end_date,
            skip_done=not args.no_skip_done,
            fetch_snapshot=not args.no_snapshot,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception(f"backfill_symbols failed: {e}")
        return 2

    _show_results(args.symbols, end_date)

    logger.success("daily_kis smoke test complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
