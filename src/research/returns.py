"""Forward return computation for cross-sectional alpha evaluation.

Definitions:
    fwd_return for horizon=N on date t:
        (close_{t+N} - close_t) / close_t
        where t and t+N are both trading days for the symbol.
        Holidays / halted days are SKIPPED, not included in the horizon
        count: horizon=5 means "5 actual trading days forward".

    high_return (only for horizon=1):
        (high_{t+1} - close_t) / close_t
        Tracks the user's existing target definition. Stored on the
        horizon=1 row only; NULL for other horizons.

Implementation strategy:
    1. Pull daily_prices for the requested symbols and date range,
       including a "tail" buffer (max_horizon trading days) past `end`
       so we can compute the forward return for the last day in `end`.
    2. For each symbol, sort by date and shift close by -horizon
       positions. This gives the "close N trading days later" without
       being fooled by holidays.
    3. Compute (shifted_close - close) / close per row.
    4. Drop rows where the shifted close is NaN (these are dates within
       horizon of the end of available data - they don't have a known
       forward return yet).
    5. Persist via upsert_forward_returns (idempotent).

Why daily_prices.close (not adj_close):
    For KR stocks we have one `close` column which is treated as the
    adjusted close (FDR/pykrx both return adjustment-aware values for
    historical ranges). If you want raw unadjusted, use daily_prices_raw
    via a future variant.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text

from src.db.connection import get_engine
from src.db.repositories import upsert_forward_returns
from src.utils.logger import logger

# Default horizon set for Phase 1 (per design decision).
DEFAULT_HORIZONS: tuple[int, ...] = (1, 5, 10, 20)


def compute_forward_returns(
    symbols: list[str],
    start: date,
    end: date,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    universe_label: str | None = None,
) -> pd.DataFrame:
    """Compute forward returns for the cross of `symbols` x business days
    in [start, end] x horizons.

    Args:
        symbols: list of 6-digit symbols.
        start: inclusive start date for the BASE date (date t). The
               computation pulls extra data after `end` to fill horizons.
        end: inclusive end date for the BASE date. The latest row
             returned will have base date == end.
        horizons: list of horizons in trading days. Each one creates
                  N additional rows per (symbol, base_date).
        universe_label: optional label stored alongside (KOSPI/KOSDAQ/
                        ALL/etc.). Just metadata for the cached table.

    Returns:
        Long-format DataFrame with columns:
            [symbol, date, horizon_days, fwd_return, high_return,
             universe, source]
        Rows whose forward close is unknown (insufficient data after t)
        are EXCLUDED. So row count may be less than
        len(symbols) * n_business_days * len(horizons).
    """
    if not symbols:
        return pd.DataFrame(
            columns=["symbol", "date", "horizon_days",
                     "fwd_return", "high_return",
                     "universe", "source"]
        )

    if start > end:
        raise ValueError(f"start {start} > end {end}")

    max_horizon = max(horizons)
    # Pull buffer = max_horizon * 2 calendar days past `end` to ensure
    # max_horizon trading days are available (weekends/holidays absorb
    # part of that buffer).
    pull_end = end + timedelta(days=max_horizon * 2 + 7)

    prices = _load_close_high(symbols, start, pull_end)
    if prices.empty:
        logger.warning(
            f"compute_forward_returns: no price data for {len(symbols)} symbols "
            f"in [{start}, {pull_end}]"
        )
        return pd.DataFrame(
            columns=["symbol", "date", "horizon_days",
                     "fwd_return", "high_return",
                     "universe", "source"]
        )

    # `prices` is sorted (symbol, date). For each symbol independently
    # we shift `close` by -horizon and compute the return. The shift
    # is by TRADING-DAY positions because the per-symbol data only
    # contains its actual trading days.
    out_frames: list[pd.DataFrame] = []
    grouped = prices.groupby("symbol", sort=False, group_keys=False)

    for h in horizons:
        shifted = grouped["close"].shift(-h)
        fwd_ret = (shifted - prices["close"]) / prices["close"]
        # high_return only on horizon == 1
        if h == 1:
            shifted_high = grouped["high"].shift(-1)
            high_ret = (shifted_high - prices["close"]) / prices["close"]
        else:
            high_ret = pd.Series([None] * len(prices), index=prices.index)

        frame = pd.DataFrame({
            "symbol": prices["symbol"].values,
            "date": prices["date"].values,
            "horizon_days": h,
            "fwd_return": fwd_ret.values,
            "high_return": high_ret.values,
        })
        # Drop rows whose forward close was NaN (not enough trading days
        # ahead). Keep rows where fwd_return is computable but
        # high_return is NaN (only happens for horizon=1 at the very end).
        frame = frame[frame["fwd_return"].notna()].copy()
        # Clip base date to [start, end] - we may have pulled earlier
        # buffer rows that don't belong to the requested base range.
        frame = frame[(frame["date"] >= start) & (frame["date"] <= end)]
        out_frames.append(frame)

    if not out_frames:
        return pd.DataFrame(
            columns=["symbol", "date", "horizon_days",
                     "fwd_return", "high_return",
                     "universe", "source"]
        )

    out = pd.concat(out_frames, ignore_index=True)
    out["universe"] = universe_label
    out["source"] = "daily_prices"

    # Type cleanup
    out["horizon_days"] = out["horizon_days"].astype("int16")
    # high_return may have object dtype after the None fill; normalize to float
    out["high_return"] = pd.to_numeric(out["high_return"], errors="coerce")

    return out[
        ["symbol", "date", "horizon_days",
         "fwd_return", "high_return", "universe", "source"]
    ]


def backfill_forward_returns(
    symbols: list[str],
    start: date,
    end: date,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    universe_label: str | None = None,
) -> int:
    """Compute forward returns and persist them.

    Returns:
        Number of rows upserted.
    """
    df = compute_forward_returns(
        symbols=symbols,
        start=start,
        end=end,
        horizons=horizons,
        universe_label=universe_label,
    )
    if df.empty:
        logger.info("backfill_forward_returns: nothing to upsert")
        return 0

    n = upsert_forward_returns(df)
    logger.success(
        f"forward_returns upserted: {n:,} rows "
        f"({len(symbols)} symbols x {len(horizons)} horizons)"
    )
    return n


def _load_close_high(
    symbols: list[str],
    start: date,
    end: date,
) -> pd.DataFrame:
    """Load (symbol, date, close, high) for the requested symbols/range.

    Drops rows with NULL close (corrupt data) and zero/negative close
    (sentinel for halted / pre-listing days seen in some feeds). Returns
    a DataFrame sorted by (symbol, date) - critical for the shift logic
    upstream.
    """
    sql = text("""
        SELECT symbol, date, close, high
          FROM daily_prices
         WHERE symbol = ANY(:syms)
           AND date BETWEEN :s AND :e
           AND close IS NOT NULL
           AND close > 0
         ORDER BY symbol, date
    """)
    with get_engine().connect() as conn:
        df = pd.read_sql(
            sql, conn,
            params={"syms": symbols, "s": start, "e": end},
        )
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["high"] = pd.to_numeric(df["high"], errors="coerce")
    return df


if __name__ == "__main__":
    # Smoke test: 2 well-known symbols, last 60 days
    from datetime import date as _date
    end = _date.today()
    start = end - timedelta(days=90)
    n = backfill_forward_returns(
        symbols=["005930", "000660"],
        start=start, end=end,
    )
    logger.info(f"Smoke test result: {n} rows upserted")
