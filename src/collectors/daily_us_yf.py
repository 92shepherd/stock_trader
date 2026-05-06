"""Daily US price collector using yfinance.

Strategy:
    yfinance's `download()` accepts a list of tickers and returns a
    multi-index DataFrame with all of them in one HTTP burst (it
    parallelizes internally). This is faster than calling per-symbol,
    but Yahoo enforces an undocumented soft-limit on batch size — too
    big a batch and rows come back partially or as NaN. Empirically
    50 symbols per batch is the sweet spot.

    So this is BATCH-iterating, not pure per-symbol:
      - Outer loop: chunks of `BATCH_SIZE` symbols
      - One yfinance call per chunk → returns ALL symbols × ALL dates
      - Reshape into long-form, upsert in bulk
      - Resume key is still per-symbol (one log row per symbol per
        end_date), so failed symbols within a batch can be retried
        individually on the next run.

What yfinance gives us:
    Open, High, Low, Close, Adj Close, Volume   (per symbol, per date)
    Dividends, Stock Splits   (separately via Ticker.history; not in
                                download() — we accept this gap and
                                leave dividend/split_ratio as 0 for now)

What we DO NOT collect here:
    - Market cap, PER, PBR — yfinance's fast_info, separate per-symbol calls
    - Pre/post-market — would need a different feed
    - Real-time — yfinance is delayed 15-20 min
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pandas as pd
import yfinance as yf
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from src.config import get_app_config
from src.db.repositories import (
    get_active_us_tickers,
    get_completed_us_symbols_in_range,
    log_collection,
    upsert_daily_prices_us,
)
from src.utils.logger import logger

COLLECTOR_NAME = "daily_us_yf"

# yfinance batch size. Yahoo's soft-limit varies; 50 is empirically safe
# and gives ~120 batches for 6,000 symbols → ~2-5 min total fetch time.
BATCH_SIZE = 50

# Expected yfinance OHLCV columns. If NONE are present, the response is
# considered broken.
_YF_COLS = {"Open", "High", "Low", "Close", "Adj Close", "Volume"}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    reraise=True,
)
def _safe_download(
    symbols: list[str],
    start: date,
    end: date,
) -> pd.DataFrame:
    """yfinance occasionally throws transient JSON/network errors. Retry."""
    # yfinance interprets `end` as exclusive, so add one day.
    return yf.download(
        tickers=symbols,
        start=start.strftime("%Y-%m-%d"),
        end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
        group_by="ticker",
        auto_adjust=False,        # keep raw Close + Adj Close separately
        progress=False,
        threads=True,
        actions=False,
    )


def _normalize_batch(
    raw: pd.DataFrame,
    symbols: list[str],
) -> dict[str, pd.DataFrame]:
    """Reshape yfinance's multi-index download into per-symbol long DFs.

    yfinance returns one of two shapes depending on len(symbols):
      - 1 symbol  → flat DataFrame, columns = [Open, High, Low, ...]
      - N symbols → MultiIndex columns: (symbol, field)

    We always normalize to {symbol: long_df_with_date_column}.
    Returns empty dict if `raw` is empty/None or has unexpected shape.
    """
    if raw is None or raw.empty:
        return {}

    out: dict[str, pd.DataFrame] = {}

    if isinstance(raw.columns, pd.MultiIndex):
        # Multi-symbol shape. Top level of the column index can be either
        # the symbol or the field, depending on yfinance version /
        # group_by argument. We requested group_by="ticker", so level 0
        # should be the symbol.
        top_level = raw.columns.get_level_values(0).unique().tolist()
        for sym in top_level:
            try:
                sub = raw[sym]
            except KeyError:
                continue
            if not isinstance(sub, pd.DataFrame) or sub.empty:
                continue
            out[sym] = sub
    else:
        # Single-symbol shape. yfinance gives us the data without a
        # symbol layer — match it back to the input.
        if len(symbols) != 1:
            logger.warning(
                f"yfinance returned flat columns but we asked for "
                f"{len(symbols)} symbols — skipping batch"
            )
            return {}
        out[symbols[0]] = raw

    # Drop rows where everything is NaN (yfinance pads non-trading days
    # with NaN when a batch spans symbols with different listing dates)
    cleaned: dict[str, pd.DataFrame] = {}
    for sym, df in out.items():
        if not (set(df.columns) & _YF_COLS):
            logger.warning(
                f"  {sym}: yfinance response missing OHLCV columns "
                f"(got {list(df.columns)}) — skipping"
            )
            continue
        df = df.dropna(how="all")
        if df.empty:
            continue
        cleaned[sym] = df

    return cleaned


def _to_long(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Convert one symbol's yfinance frame to our daily_prices_us schema."""
    df = df.rename(columns={
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Adj Close": "adj_close",
        "Volume": "volume",
    })

    # Date is the index — promote to column
    df = df.reset_index().rename(columns={"Date": "date", "index": "date"})
    df["symbol"] = symbol
    df["source"] = "yfinance"

    # yfinance's `download()` doesn't include dividends/splits even with
    # actions=True in some versions; leave as 0 and let a future
    # collector backfill via Ticker.history if we ever need it.
    df["dividend"] = 0
    df["split_ratio"] = 0

    # Type fixes
    df["date"] = pd.to_datetime(df["date"]).dt.date

    # ------------------------------------------------------------------
    # CRITICAL: volume 컬럼 타입 변환
    # ------------------------------------------------------------------
    # yfinance는 NaN을 표현하기 위해 Volume을 float64로 반환한다.
    # 그 결과 4005800같은 정수도 4005800.0으로 저장되고,
    # to_csv() 시 "4005800.0" 문자열이 되어 PostgreSQL BIGINT가 거부한다.
    # 해결: pandas의 nullable Int64로 캐스팅 (NaN은 <NA>로 유지되고
    # to_csv()에서 na_rep='\\N'에 의해 PostgreSQL NULL로 들어간다).
    if "volume" in df.columns:
        df["volume"] = (
            pd.to_numeric(df["volume"], errors="coerce")
            .round()
            .astype("Int64")
        )

    # Drop rows where close is missing or zero
    df = df[df["close"].notna() & (df["close"] > 0)]

    keep = [
        "symbol", "date", "open", "high", "low", "close",
        "adj_close", "volume", "dividend", "split_ratio", "source",
    ]
    keep = [c for c in keep if c in df.columns]
    return df[keep].copy()


def _collect_one_batch(
    symbols: list[str],
    start_date: date,
    end_date: date,
) -> dict[str, int]:
    """Fetch & upsert one batch. Returns {symbol: rows_upserted}.

    Symbols that came back empty (holidays, halted, listing too recent)
    appear with rows=0. Symbols that errored out aren't in the result.
    """
    raw = _safe_download(symbols, start_date, end_date)
    per_symbol = _normalize_batch(raw, symbols)

    result: dict[str, int] = {}
    if not per_symbol:
        return {sym: 0 for sym in symbols}

    # Upsert per-symbol so a malformed row in one symbol doesn't taint
    # the whole batch's upsert. Cost is N round-trips per batch but each
    # is small; total time is dominated by the HTTP fetch upstream.
    for sym, df in per_symbol.items():
        long_df = _to_long(df, sym)
        if long_df.empty:
            result[sym] = 0
            continue
        result[sym] = upsert_daily_prices_us(long_df)

    # Symbols not present in `per_symbol` got nothing back from yfinance
    for sym in symbols:
        result.setdefault(sym, 0)

    return result


def backfill_symbols(
    symbols: list[str],
    start_date: date | None = None,
    end_date: date | None = None,
    days: int | None = None,
    skip_done: bool = True,
    consecutive_fail_limit: int = 5,
) -> dict[str, int]:
    """Backfill US daily prices for a list of symbols over a date range.

    Args:
        symbols: list of yfinance-form tickers (e.g. ["AAPL", "BRK-B"]).
        start_date / end_date / days: same semantics as Korea collectors.
        skip_done: if True (default), symbols already logged as success
            for this exact `end_date` are skipped.
        consecutive_fail_limit: abort if this many BATCHES fail in a row
            (5 batches = 250 symbols of consecutive failure). Set <=0
            to disable.

    Returns counts: {ok, failed, empty, skipped, total_rows}.
    """
    cfg = get_app_config()
    end_date = end_date or date.today()
    if start_date is None:
        # Use Korea's daily backfill_days as a sane default until we
        # add a us_daily section to settings.yaml
        days = days or cfg.collection.daily.backfill_days
        start_date = end_date - timedelta(days=days)

    if not symbols:
        logger.warning("backfill_symbols called with empty symbol list")
        return {"ok": 0, "failed": 0, "empty": 0, "skipped": 0, "total_rows": 0}

    skipped_done = 0
    if skip_done:
        already = get_completed_us_symbols_in_range(
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
        f"yfinance backfill: {len(symbols)} symbol(s), "
        f"{start_date} → {end_date}, batch_size={BATCH_SIZE}"
    )

    ok = 0
    failed = 0
    empty = 0
    total_rows = 0
    consecutive_failures = 0

    batches = [
        symbols[i:i + BATCH_SIZE]
        for i in range(0, len(symbols), BATCH_SIZE)
    ]

    request_delay = cfg.collection.daily.request_delay  # reuse for now

    for batch in tqdm(batches, desc="us yf backfill"):
        t0 = time.time()
        try:
            result = _collect_one_batch(batch, start_date, end_date)
            dur_per = max(1, int((time.time() - t0) * 1000 / max(1, len(batch))))
            for sym, rows in result.items():
                if rows == 0:
                    empty += 1
                    log_collection(
                        COLLECTOR_NAME, end_date, "skipped",
                        symbol=sym, duration_ms=dur_per,
                    )
                else:
                    ok += 1
                    total_rows += rows
                    log_collection(
                        COLLECTOR_NAME, end_date, "success",
                        symbol=sym, rows_inserted=rows, duration_ms=dur_per,
                    )
            consecutive_failures = 0
        except Exception as e:
            failed += len(batch)
            consecutive_failures += 1
            dur_per = max(1, int((time.time() - t0) * 1000 / max(1, len(batch))))
            logger.error(f"  batch [{batch[0]}…{batch[-1]}]: {e}")
            for sym in batch:
                log_collection(
                    COLLECTOR_NAME, end_date, "failed",
                    symbol=sym, error_message=str(e)[:500],
                    duration_ms=dur_per,
                )
            if (
                consecutive_fail_limit > 0
                and consecutive_failures >= consecutive_fail_limit
            ):
                logger.error(
                    f"Aborting: {consecutive_failures} consecutive batch failures. "
                    "Likely a Yahoo outage or rate-limit. Re-run to resume."
                )
                break

        time.sleep(request_delay)

    logger.success(
        f"yfinance backfill done — ok: {ok}, failed: {failed}, "
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
    exchanges: list[str] | None = None,
    security_types: list[str] | None = None,
    skip_done: bool = True,
) -> dict[str, int]:
    """Backfill EVERY active US ticker in the DB.

    Performance note: ~6,000 symbols / 50 per batch × ~1-3 sec per batch
    = roughly 5-15 minutes for a fresh backfill on a fast network.
    Subsequent runs (with skip_done=True) take seconds when there's
    nothing new to fetch.

    Prerequisite: the `tickers_us` table must be populated. Run
    `collect_us_tickers()` first.

    Args:
        start_date / end_date / days: same as backfill_symbols.
        exchanges: filter to specific exchanges (e.g. ["NASDAQ", "NYSE"]).
        security_types: filter to specific types (e.g. ["COMMON", "ETF"]).
        skip_done: pass-through.
    """
    tickers = get_active_us_tickers(
        exchanges=exchanges,
        security_types=security_types,
    )
    symbols = [t.symbol for t in tickers]
    if not symbols:
        logger.warning(
            "Active US universe is empty — did you populate the "
            "`tickers_us` table yet? Run collect_us_tickers() first."
        )
        return {"ok": 0, "failed": 0, "empty": 0, "skipped": 0, "total_rows": 0}
    logger.info(f"Active US universe: {len(symbols)} ticker(s)")
    return backfill_symbols(
        symbols,
        start_date=start_date,
        end_date=end_date,
        days=days,
        skip_done=skip_done,
    )


if __name__ == "__main__":
    # Smoke test: AAPL + MSFT, last 30 days
    backfill_symbols(["AAPL", "MSFT"], days=30)
