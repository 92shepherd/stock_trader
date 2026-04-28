"""<COLLECTOR_PURPOSE — one line>.

Strategy:
    <Explain WHY symbol-iterating is right for this upstream. Mention:
    - Upstream API shape (per-symbol, multi-period)
    - Approximate cost per backfill (HTTP calls × symbols)
    - Why the expected request_delay value
    - What does NOT come back from this source (so other collectors
      know they need to fill those columns)>

What this module does NOT provide:
    - <column1> — <reason>
    - <column2> — <reason>
    Those columns are left as NULL in <table>. Combine with <other_collector>
    on the same PK and ON CONFLICT DO UPDATE will fill them in.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import <upstream_lib>  # TODO: replace
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from src.config import get_app_config
from src.db.repositories import (
    get_active_tickers,
    get_completed_symbols_in_range,
    log_collection,
    upsert_<table>,  # TODO: replace
)
from src.utils.logger import logger

COLLECTOR_NAME = "<snake_case_name>"  # TODO: replace; must match filename stem

# Expected columns from upstream. If NONE present, response is treated as
# broken (upstream outage / scraper format change) and the symbol is skipped.
_EXPECTED_COLS = {"<col1>", "<col2>", "<col3>"}  # TODO: adjust


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _safe_fetch(symbol: str, start: date, end: date) -> pd.DataFrame:
    """Call upstream with retry. <Lib> occasionally throws on transient
    network/parse errors — exponential backoff usually clears it."""
    # TODO: replace with the real upstream call
    return <upstream_lib>.<function>(
        symbol,
        start.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
    )


def _normalize(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Shape upstream response to match <table> column conventions.

    Returns empty DF on malformed/empty input rather than raising, so the
    caller's loop can continue.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    # Defensive: verify the response actually has the expected columns.
    if not (set(df.columns) & _EXPECTED_COLS):
        logger.warning(
            f"  {symbol} upstream response has unexpected columns: "
            f"{list(df.columns)} — skipping"
        )
        return pd.DataFrame()

    # Rename to match table schema
    df = df.rename(columns={
        "<UpstreamCol1>": "<table_col1>",
        # ...
    })

    # Promote index to column if needed
    df = df.reset_index().rename(columns={"<IndexName>": "<table_col>"})
    df["symbol"] = symbol

    # Keep only known columns
    keep = ["symbol", "<time_col>", "<col1>", "<col2>"]  # TODO: adjust
    keep = [c for c in keep if c in df.columns]
    df = df[keep].copy()

    # Type coercions
    # df["<time_col>"] = pd.to_datetime(df["<time_col>"]).dt.date

    # Drop rows where the key metric is null/zero
    # df = df[df["<key>"].notna() & (df["<key>"] > 0)]

    return df


def collect_one_symbol(
    symbol: str,
    start_date: date,
    end_date: date,
) -> int:
    """Fetch one symbol's data for [start_date, end_date] and upsert.

    Returns the number of rows upserted. Raises on a persistent fetch
    failure (after retries exhausted).
    """
    df = _safe_fetch(symbol, start_date, end_date)
    df = _normalize(df, symbol)
    if df.empty:
        return 0
    return upsert_<table>(df)


def backfill_symbols(
    symbols: list[str],
    start_date: date | None = None,
    end_date: date | None = None,
    days: int | None = None,
    skip_done: bool = True,
    consecutive_fail_limit: int = 20,
) -> dict[str, int]:
    """Backfill <data> for a list of symbols over a date range.

    Args:
        symbols: list of 6-digit KRX codes.
        start_date: inclusive start. Defaults to end_date - days.
        end_date: inclusive end. Defaults to today.
        days: calendar days back from end_date. Defaults to config.
        skip_done: if True (default), symbols already logged as success
            for this exact `end_date` are skipped — enables resume.
        consecutive_fail_limit: abort if this many fail in a row.

    Returns:
        dict with keys: ok, failed, empty, skipped, total_rows.
    """
    cfg = get_app_config()
    end_date = end_date or date.today()
    if start_date is None:
        days = days or cfg.collection.<grain>.backfill_days  # TODO: adjust grain
        start_date = end_date - timedelta(days=days)

    if not symbols:
        logger.warning("backfill_symbols called with empty symbol list")
        return {"ok": 0, "failed": 0, "empty": 0, "skipped": 0, "total_rows": 0}

    # Resume support: drop symbols already completed for this exact end_date.
    skipped_done = 0
    if skip_done:
        already = get_completed_symbols_in_range(
            COLLECTOR_NAME, start_date, end_date
        )
        if already:
            before = len(symbols)
            symbols = [s for s in symbols if s not in already]
            skipped_done = before - len(symbols)
            logger.info(
                f"Resume: {skipped_done} symbol(s) already completed for "
                f"end_date={end_date}, {len(symbols)} remaining"
            )

    if not symbols:
        logger.success("Nothing to backfill — every symbol already done.")
        return {
            "ok": 0, "failed": 0, "empty": 0,
            "skipped": skipped_done, "total_rows": 0,
        }

    logger.info(
        f"<COLLECTOR> backfill: {len(symbols)} symbol(s), "
        f"{start_date} → {end_date}"
    )

    ok = 0
    failed = 0
    empty = 0
    total_rows = 0
    consecutive_failures = 0

    for sym in tqdm(symbols, desc="<descriptive label>"):  # TODO: adjust label
        t0 = time.time()
        try:
            rows = collect_one_symbol(sym, start_date, end_date)
            dur = int((time.time() - t0) * 1000)
            if rows == 0:
                empty += 1
                # Log as 'skipped' (no data) — NOT counted as success,
                # so the next resumed run will retry this symbol.
                log_collection(
                    COLLECTOR_NAME, end_date, "skipped",
                    symbol=sym, duration_ms=dur,
                )
            else:
                ok += 1
                total_rows += rows
                log_collection(
                    COLLECTOR_NAME, end_date, "success",
                    symbol=sym, rows_inserted=rows, duration_ms=dur,
                )
            consecutive_failures = 0
        except Exception as e:
            failed += 1
            consecutive_failures += 1
            dur = int((time.time() - t0) * 1000)
            logger.error(f"  {sym}: {e}")
            log_collection(
                COLLECTOR_NAME, end_date, "failed",
                symbol=sym, error_message=str(e)[:500], duration_ms=dur,
            )
            if (
                consecutive_fail_limit > 0
                and consecutive_failures >= consecutive_fail_limit
            ):
                logger.error(
                    f"Aborting: {consecutive_failures} consecutive failures. "
                    "Likely a network/source issue. Re-run to resume."
                )
                break
        time.sleep(cfg.collection.<grain>.request_delay)  # TODO: adjust grain

    logger.success(
        f"<COLLECTOR> backfill done — ok: {ok}, failed: {failed}, "
        f"empty: {empty}, skipped(done): {skipped_done}, "
        f"total rows: {total_rows:,}"
    )
    return {
        "ok": ok,
        "failed": failed,
        "empty": empty,
        "skipped": skipped_done,
        "total_rows": total_rows,
    }


def backfill_active_universe(
    start_date: date | None = None,
    end_date: date | None = None,
    days: int | None = None,
    markets: list[str] | None = None,
    skip_done: bool = True,
) -> dict[str, int]:
    """Backfill EVERY active (non-delisted) ticker in the DB.

    Performance note: ~1 HTTP call per symbol. Plan accordingly.

    Prerequisite: the `tickers` table must be populated.
    """
    cfg = get_app_config()
    tickers = get_active_tickers(markets or cfg.markets)
    symbols = [t.symbol for t in tickers]
    if not symbols:
        logger.warning(
            "Active universe is empty — populate `tickers` first."
        )
        return {"ok": 0, "failed": 0, "empty": 0, "skipped": 0, "total_rows": 0}
    logger.info(f"Active universe: {len(symbols)} ticker(s)")
    return backfill_symbols(
        symbols,
        start_date=start_date,
        end_date=end_date,
        days=days,
        skip_done=skip_done,
    )


if __name__ == "__main__":
    # Smoke test: 2 well-known symbols, last 30 days
    backfill_symbols(["005930", "000660"], days=30)
