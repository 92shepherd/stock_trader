"""Daily price collector using pykrx.

Strategy:
    pykrx has two kinds of APIs:
      (a) Per-symbol, multi-date OHLCV: get_market_ohlcv(start, end, symbol)
      (b) Per-date, all-symbols OHLCV: get_market_ohlcv(date, market)

    For backfilling 1 year across 2,600 symbols:
      - (a) means ~2,600 calls. Simple but slow.
      - (b) means ~245 calls (one per trading day). MUCH faster and
            also gives us market-cap / investor-flow / fundamentals
            as separate per-date calls.

    We use strategy (b): iterate trading days, fetch all-symbol OHLCV
    plus cap/investor/fundamental data, merge, then bulk-upsert.

    Data per day, for both KOSPI and KOSDAQ:
      - get_market_ohlcv(date, market=...)              → OHLCV
      - get_market_cap(date, market=...)                → market_cap / shares
      - get_market_fundamental(date, market=...)        → PER / PBR / DIV
      - get_market_trading_value_by_investor(...)       → investor flows
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pandas as pd
from pykrx import stock
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from src.config import get_app_config
from src.db.repositories import (
    get_missing_dates,
    log_collection,
    upsert_daily_prices,
)
from src.utils.logger import logger

COLLECTOR_NAME = "daily_pykrx"

# Expected Korean column names from pykrx. If NONE of these are present, the
# response is considered invalid (KRX server hiccup) and we skip the date.
_OHLCV_KR_COLS = {"시가", "고가", "저가", "종가", "거래량", "거래대금"}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _safe_call(func, *args, **kwargs):
    """pykrx occasionally throws transient network errors. Retry with backoff."""
    return func(*args, **kwargs)


def _rename_if_present(df: pd.DataFrame, mapping: dict[str, str]) -> pd.DataFrame:
    """Rename only columns that actually exist. Returns a copy with renamed cols
    and ONLY those target columns present (unknown target columns are dropped).
    """
    existing = {k: v for k, v in mapping.items() if k in df.columns}
    df = df.rename(columns=existing)
    keep = [v for v in existing.values()]
    return df[keep].copy() if keep else pd.DataFrame()


def _fetch_one_day(target: date, market: str) -> pd.DataFrame:
    """Fetch all-symbol data for one market on one date. Returns a merged DF.

    Returns an empty DataFrame (not raises) when:
      - KRX returned no data (holiday, or transient API issue)
      - Response is malformed (missing all expected Korean columns)
    """
    date_s = target.strftime("%Y%m%d")

    # 1) OHLCV  → index: symbol
    try:
        ohlcv = _safe_call(stock.get_market_ohlcv, date_s, market=market)
    except Exception as e:
        logger.warning(f"  {target} [{market}] OHLCV fetch failed: {e}")
        return pd.DataFrame()

    if ohlcv is None or ohlcv.empty:
        return pd.DataFrame()

    # Defensive: verify the response actually has Korean OHLCV columns.
    # When KRX serves a broken/empty JSON, pykrx sometimes returns a DF with
    # unexpected columns instead of raising.
    if not (set(ohlcv.columns) & _OHLCV_KR_COLS):
        logger.warning(
            f"  {target} [{market}] OHLCV has unexpected columns: "
            f"{list(ohlcv.columns)} — treating as empty"
        )
        return pd.DataFrame()

    ohlcv = _rename_if_present(
        ohlcv,
        {
            "시가": "open",
            "고가": "high",
            "저가": "low",
            "종가": "close",
            "거래량": "volume",
            "거래대금": "value",
        },
    )

    if ohlcv.empty or "close" not in ohlcv.columns:
        return pd.DataFrame()

    # 2) Market cap  → adds market_cap, shares (best-effort; skip on failure)
    try:
        cap = _safe_call(stock.get_market_cap, date_s, market=market)
        if cap is not None and not cap.empty:
            cap = _rename_if_present(
                cap, {"시가총액": "market_cap", "상장주식수": "shares"}
            )
            if not cap.empty:
                ohlcv = ohlcv.join(cap, how="left")
    except Exception as e:
        logger.warning(f"  {target} [{market}] market_cap fetch failed: {e}")

    # 3) Fundamentals → PER / PBR / DIV (best-effort)
    try:
        fund = _safe_call(stock.get_market_fundamental, date_s, market=market)
        if fund is not None and not fund.empty:
            fund = _rename_if_present(
                fund, {"PER": "per", "PBR": "pbr", "DIV": "dividend_yield"}
            )
            if not fund.empty:
                ohlcv = ohlcv.join(fund, how="left")
    except Exception as e:
        logger.warning(f"  {target} [{market}] fundamentals fetch failed: {e}")

    # 4) Finalize
    ohlcv = ohlcv.reset_index().rename(columns={"티커": "symbol"})
    if "symbol" not in ohlcv.columns:
        # Index wasn't named "티커" — bail out rather than writing bogus data
        logger.warning(
            f"  {target} [{market}] response missing '티커' index — skipping"
        )
        return pd.DataFrame()
    ohlcv["date"] = target

    return ohlcv


def collect_daily_for_date(target: date) -> int:
    """Collect one trading day across all configured markets. Returns row count.

    Errors in individual markets are logged but do NOT abort other markets.
    Only raises if ALL markets fail with exceptions (transient outage).
    """
    cfg = get_app_config()
    frames: list[pd.DataFrame] = []
    errors: list[tuple[str, Exception]] = []

    for market in cfg.markets:
        try:
            df = _fetch_one_day(target, market)
            if not df.empty:
                frames.append(df)
                logger.debug(f"  {target} [{market}]: {len(df)} rows")
            time.sleep(cfg.collection.daily.request_delay)
        except Exception as e:
            logger.error(f"  {target} [{market}] failed: {e}")
            errors.append((market, e))

    # If every market raised an exception, surface it (transient KRX outage)
    if errors and len(errors) == len(cfg.markets):
        markets = ", ".join(m for m, _ in errors)
        raise RuntimeError(
            f"All markets failed for {target} ({markets}): {errors[0][1]}"
        )

    if not frames:
        return 0

    merged = pd.concat(frames, ignore_index=True)
    # Drop rows where close is missing or zero (holidays, halted stocks)
    merged = merged[merged["close"].notna() & (merged["close"] > 0)]

    if merged.empty:
        return 0

    rows = upsert_daily_prices(merged)
    return rows


def backfill(
    start_date: date | None = None,
    end_date: date | None = None,
    days: int | None = None,
    skip_done: bool = True,
) -> None:
    """Backfill daily prices for a date range.

    Args:
        start_date: first date (inclusive). Defaults to end_date - days.
        end_date: last date (inclusive). Defaults to today.
        days: calendar days to go back from end_date. Defaults to config.
        skip_done: if True, skip dates already logged as success.

    Usage:
        backfill(days=400)                    # last 400 calendar days
        backfill(start_date=date(2024,1,1))   # from Jan 1 2024 to today
        backfill(start_date=..., end_date=...)
    """
    cfg = get_app_config()
    end_date = end_date or date.today()
    if start_date is None:
        days = days or cfg.collection.daily.backfill_days
        start_date = end_date - timedelta(days=days)

    logger.info(f"Backfilling daily prices: {start_date} → {end_date}")

    # Build candidate weekday list; pykrx returns empty on holidays.
    if skip_done:
        targets = get_missing_dates(COLLECTOR_NAME, start_date, end_date)
        logger.info(
            f"Candidate trading days (not yet successful): {len(targets)}"
        )
    else:
        targets = []
        d = start_date
        while d <= end_date:
            if d.weekday() < 5:  # Mon-Fri
                targets.append(d)
            d += timedelta(days=1)
        logger.info(f"Candidate trading days: {len(targets)}")

    if not targets:
        logger.success("Nothing to backfill — all target dates already done.")
        return

    total_rows = 0
    consecutive_failures = 0
    for target in tqdm(targets, desc="daily backfill"):
        t0 = time.time()
        try:
            rows = collect_daily_for_date(target)
            dur = int((time.time() - t0) * 1000)
            if rows == 0:
                log_collection(
                    COLLECTOR_NAME, target, "skipped", duration_ms=dur
                )
            else:
                log_collection(
                    COLLECTOR_NAME, target, "success",
                    rows_inserted=rows, duration_ms=dur,
                )
                total_rows += rows
            consecutive_failures = 0
        except Exception as e:
            dur = int((time.time() - t0) * 1000)
            logger.error(f"Failed on {target}: {e}")
            log_collection(
                COLLECTOR_NAME, target, "failed",
                error_message=str(e)[:500], duration_ms=dur,
            )
            consecutive_failures += 1
            # Circuit breaker: if many consecutive failures, likely a KRX
            # outage — abort rather than pointlessly hammering the server.
            if consecutive_failures >= 10:
                logger.error(
                    f"Aborting: {consecutive_failures} consecutive failures. "
                    "Likely a KRX server issue. Re-run later to resume."
                )
                break

    logger.success(f"Backfill done. Total rows upserted: {total_rows:,}")


if __name__ == "__main__":
    backfill()
