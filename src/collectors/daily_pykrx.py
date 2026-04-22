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
    log_collection,
    upsert_daily_prices,
)
from src.utils.logger import logger

COLLECTOR_NAME = "daily_pykrx"


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _safe_call(func, *args, **kwargs):
    """pykrx occasionally throws transient network errors. Retry with backoff."""
    return func(*args, **kwargs)


def _fetch_one_day(target: date, market: str) -> pd.DataFrame:
    """Fetch all-symbol data for one market on one date. Returns a merged DF."""
    date_s = target.strftime("%Y%m%d")

    # 1) OHLCV  → index: symbol
    ohlcv = _safe_call(stock.get_market_ohlcv, date_s, market=market)
    if ohlcv is None or ohlcv.empty:
        return pd.DataFrame()

    ohlcv = ohlcv.rename(
        columns={
            "시가": "open",
            "고가": "high",
            "저가": "low",
            "종가": "close",
            "거래량": "volume",
            "거래대금": "value",
            # "등락률" also exists but we skip it
        }
    )[["open", "high", "low", "close", "volume", "value"]]

    # 2) Market cap  → adds market_cap, shares
    cap = _safe_call(stock.get_market_cap, date_s, market=market)
    if cap is not None and not cap.empty:
        cap = cap.rename(columns={"시가총액": "market_cap", "상장주식수": "shares"})[
            ["market_cap", "shares"]
        ]
        ohlcv = ohlcv.join(cap, how="left")

    # 3) Fundamentals → PER / PBR / DIV
    fund = _safe_call(stock.get_market_fundamental, date_s, market=market)
    if fund is not None and not fund.empty:
        # columns: BPS, PER, PBR, EPS, DIV, DPS
        keep = {"PER": "per", "PBR": "pbr", "DIV": "dividend_yield"}
        fund = fund[[c for c in keep if c in fund.columns]].rename(columns=keep)
        ohlcv = ohlcv.join(fund, how="left")

    # 4) Finalize
    ohlcv = ohlcv.reset_index().rename(columns={"티커": "symbol"})
    ohlcv["date"] = target

    # investor flows: optional, API can be finicky — skipped in v1 backfill,
    # collected separately on demand. Leaving columns NULL.

    # Type fix: volume/value/cap/shares can be NaN → keep as NaN for Parquet,
    # but COPY needs NULL (\N). pandas to_csv handles NaN→\N via na_rep.
    return ohlcv


def collect_daily_for_date(target: date) -> int:
    """Collect one trading day across all configured markets. Returns row count."""
    cfg = get_app_config()
    frames = []
    for market in cfg.markets:
        try:
            df = _fetch_one_day(target, market)
            if not df.empty:
                frames.append(df)
                logger.debug(f"  {target} [{market}]: {len(df)} rows")
            time.sleep(cfg.collection.daily.request_delay)
        except Exception as e:
            logger.error(f"  {target} [{market}] failed: {e}")
            raise

    if not frames:
        return 0

    merged = pd.concat(frames, ignore_index=True)
    # Drop rows where OHLCV is entirely zero (holidays reported as empty by some markets)
    merged = merged[merged["close"].notna() & (merged["close"] > 0)]

    if merged.empty:
        return 0

    rows = upsert_daily_prices(merged)
    return rows


def backfill(
    start_date: date | None = None,
    end_date: date | None = None,
    days: int | None = None,
) -> None:
    """Backfill daily prices for a date range.

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

    # Weekdays only; pykrx returns empty on holidays (we skip via close>0 filter)
    targets: list[date] = []
    d = start_date
    while d <= end_date:
        if d.weekday() < 5:  # Mon-Fri
            targets.append(d)
        d += timedelta(days=1)

    logger.info(f"Candidate trading days: {len(targets)}")

    total_rows = 0
    for target in tqdm(targets, desc="daily backfill"):
        t0 = time.time()
        try:
            rows = collect_daily_for_date(target)
            dur = int((time.time() - t0) * 1000)
            if rows == 0:
                log_collection(COLLECTOR_NAME, target, "skipped", duration_ms=dur)
            else:
                log_collection(
                    COLLECTOR_NAME, target, "success",
                    rows_inserted=rows, duration_ms=dur,
                )
                total_rows += rows
        except Exception as e:
            dur = int((time.time() - t0) * 1000)
            logger.error(f"Failed on {target}: {e}")
            log_collection(
                COLLECTOR_NAME, target, "failed",
                error_message=str(e)[:500], duration_ms=dur,
            )

    logger.success(f"Backfill done. Total rows upserted: {total_rows:,}")


if __name__ == "__main__":
    backfill()
