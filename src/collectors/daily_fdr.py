"""Daily price collector using FinanceDataReader.

Purpose:
    FinanceDataReader (fdr) was originally a COMPLEMENTARY/FALLBACK
    source to the primary pykrx-based collector. As of the 2026-01
    KRX Data Marketplace login policy change, fdr is also serviceable
    as the PRIMARY source for full-universe daily backfills when KRX
    credentials are unavailable.

    Use this module when:
      - You can't (or prefer not to) authenticate against KRX, so the
        pykrx collector is off the table.
      - A specific symbol's data is missing or suspect in daily_prices
        and you want to re-fetch just that symbol over a long range.
      - You need adjusted-price OHLCV (fdr returns adjusted prices by
        default; pykrx returns raw unless `adjusted=True` is passed).

Strategy:
    fdr's API shape is per-symbol, multi-date:
        fdr.DataReader(symbol, start, end) -> DataFrame(Open, High, Low,
                                                        Close, Volume, Change)
    Unlike pykrx, there is NO per-date, all-symbols API. So this module
    iterates symbols, not dates. That's ~1 HTTP call per symbol per
    range (vs pykrx's ~1 call per date). For ~2,600 KOSPI+KOSDAQ symbols
    and a 400-day range, plan on 1–3 hours total — hence the
    `skip_done` resume flag and the consecutive-failure circuit breaker.

What this module does NOT provide:
    - Trade value in KRW (거래대금) — fdr does not expose it
    - Market cap / shares outstanding
    - PER / PBR / dividend yield
    - Investor flows (foreign / institution / individual net)
    Those columns are left as NULL in daily_prices. If you later run the
    pykrx collector on the same (symbol, date), pykrx's richer data will
    overwrite via the ON CONFLICT upsert.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import FinanceDataReader as fdr
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from src.config import get_app_config
from src.db.repositories import (
    get_active_tickers,
    get_completed_symbols_in_range,
    log_collection,
    upsert_daily_prices,
)
from src.utils.logger import logger

COLLECTOR_NAME = "daily_fdr"

# Expected English column names from fdr. Used as a sanity check; if NONE
# of these are present, the response is treated as broken (upstream scraper
# change on Naver/Yahoo) and the symbol is skipped.
_FDR_COLS = {"Open", "High", "Low", "Close", "Volume"}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _safe_fetch(symbol: str, start: date, end: date) -> pd.DataFrame:
    """Call fdr.DataReader with retry. fdr occasionally throws on transient
    network/parse errors — exponential backoff usually clears it."""
    return fdr.DataReader(
        symbol,
        start.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
    )


def _normalize(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Shape an fdr response to match daily_prices column conventions.

    Returns empty DF on malformed/empty input rather than raising, so the
    caller's loop can continue.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    # Defensive: verify it actually looks like an OHLCV frame. fdr is a
    # scraper and occasionally returns junk on upstream hiccups.
    if not (set(df.columns) & _FDR_COLS):
        logger.warning(
            f"  {symbol} fdr response has unexpected columns: "
            f"{list(df.columns)} — skipping"
        )
        return pd.DataFrame()

    df = df.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )

    # Date comes in as the index — promote to a column
    df = df.reset_index().rename(columns={"Date": "date", "index": "date"})
    df["symbol"] = symbol

    # Keep only columns daily_prices understands. Missing ones (value,
    # market_cap, fundamentals, investor flows) will be NULLed by the
    # upsert layer.
    keep = ["symbol", "date", "open", "high", "low", "close", "volume"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].copy()

    # fdr's Date is a pandas Timestamp; convert to python date to match the
    # daily_prices.date column type.
    df["date"] = pd.to_datetime(df["date"]).dt.date

    # Drop rows with null or zero close (holidays, halted names)
    df = df[df["close"].notna() & (df["close"] > 0)]

    return df


def collect_one_symbol(
    symbol: str,
    start_date: date,
    end_date: date,
) -> int:
    """Fetch one symbol's daily prices for [start_date, end_date] and upsert.

    Returns the number of rows upserted. Raises on a persistent fetch
    failure (after retries exhausted).
    """
    df = _safe_fetch(symbol, start_date, end_date)
    df = _normalize(df, symbol)
    if df.empty:
        return 0
    return upsert_daily_prices(df)


def backfill_symbols(
    symbols: list[str],
    start_date: date | None = None,
    end_date: date | None = None,
    days: int | None = None,
    skip_done: bool = True,
    consecutive_fail_limit: int = 20,
) -> dict[str, int]:
    """Backfill daily prices for a list of symbols over a date range.

    Designed for targeted re-collection AND — with `skip_done=True` —
    full-universe backfills that can resume after interruption.
    Even so, the per-symbol HTTP cost (~1 call/symbol) means a fresh
    full-universe backfill takes hours; pykrx remains the better choice
    when it's available.

    Args:
        symbols: list of 6-digit KRX codes (e.g., ["005930", "000660"]).
        start_date: inclusive start. Defaults to end_date - days.
        end_date: inclusive end. Defaults to today.
        days: calendar days back from end_date. Defaults to config.
        skip_done: if True (default), symbols already logged as success
            for this exact `end_date` are skipped — enables resuming an
            interrupted backfill on the same range.
        consecutive_fail_limit: abort if this many symbols fail in a row
            (likely a network/source outage; pointless to keep hammering).
            Set <=0 to disable.

    Returns:
        dict with keys: ok, failed, empty, skipped, total_rows.
    """
    cfg = get_app_config()
    end_date = end_date or date.today()
    if start_date is None:
        days = days or cfg.collection.daily.backfill_days
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
        f"FDR backfill: {len(symbols)} symbol(s), {start_date} → {end_date}"
    )

    ok = 0
    failed = 0
    empty = 0
    total_rows = 0
    consecutive_failures = 0

    for sym in tqdm(symbols, desc="fdr backfill"):
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
            # Circuit breaker: many consecutive failures → likely a
            # network/source outage. Stop and let user re-run later.
            if (
                consecutive_fail_limit > 0
                and consecutive_failures >= consecutive_fail_limit
            ):
                logger.error(
                    f"Aborting: {consecutive_failures} consecutive failures. "
                    "Likely a network/source issue. Re-run to resume."
                )
                break
        time.sleep(cfg.collection.daily.request_delay)

    logger.success(
        f"FDR backfill done — ok: {ok}, failed: {failed}, "
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
    """Backfill EVERY active (non-delisted) ticker in the DB via FDR.

    Suitable for full-universe daily-price collection when pykrx is
    unavailable (e.g. KRX login policy issues). With `skip_done=True`
    (default), interrupted runs can be resumed by simply re-invoking
    this function with the same `end_date` — already-successful symbols
    are skipped automatically.

    Performance note: ~1 HTTP call per symbol. Expect roughly
        len(symbols) * (request_delay + ~1–3s network) seconds total.
        For ~2,600 KOSPI+KOSDAQ symbols and the default 0.3s delay,
        plan on 1–3 hours for a fresh full-universe backfill.

    Prerequisite: the `tickers` table must be populated. Run
    `collect_tickers_fdr()` (or the pipeline's step 1) before calling
    this for the first time — otherwise it returns immediately with
    nothing to do.

    Args:
        start_date / end_date / days: same semantics as `backfill_symbols`.
        markets: subset of ["KOSPI", "KOSDAQ", ...]. Defaults to config.
        skip_done: pass-through to `backfill_symbols`.
    """
    cfg = get_app_config()
    tickers = get_active_tickers(markets or cfg.markets)
    symbols = [t.symbol for t in tickers]
    if not symbols:
        logger.warning(
            "Active universe is empty — did you populate the `tickers` "
            "table yet? Run collect_tickers_fdr() first."
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
    # Smoke test: Samsung + SK Hynix, last 30 days
    backfill_symbols(["005930", "000660"], days=30)
