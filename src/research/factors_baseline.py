"""Baseline factors from price data only.

These are the canonical "starter" factors used in every quant intro
text. They serve two purposes here:

    1. Sanity-check the Phase 1 evaluation infrastructure end-to-end
       before any complex factor is introduced. If momentum_20d doesn't
       show a positive IC on KOSPI / KOSDAQ, something is wrong with
       the pipeline (not with momentum).

    2. Provide the FIRST honest baseline that a future ML model must
       beat. ML models that can't beat momentum_20d aren't worth
       running in production.

Factors implemented:

    momentum_<N>d      - past N-day total return (excluding most-recent
                         day to avoid 1-day reversal). 'classic 12-1
                         momentum' if N=252 with skip=21; here we
                         support shorter forms (20, 60, 120).

    reversal_<N>d      - negative past N-day return. Short-term reversal
                         is well-documented in Korean markets at 1-week
                         to 1-month horizons. Sign is flipped so that
                         "high signal -> expected outperform".

    volatility_<N>d    - past N-day std of daily returns. We RETURN
                         negative volatility as the signal because
                         low-vol stocks tend to outperform on a
                         risk-adjusted basis (Frazzini-Pedersen).

Returns from each function:
    Long DataFrame with columns: symbol, date, raw_value.
    The factor name (e.g. 'momentum_20d') is a function argument so the
    caller can label it explicitly when persisting.

Why not use TA-Lib / pandas_ta:
    These factors are simple enough that explicit pandas operations are
    clearer and require no extra dependencies. The factor_eval driver
    can wrap any of them; new factors can follow the same shape.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text

from src.db.connection import get_engine


def momentum(
    symbols: list[str],
    start: date,
    end: date,
    lookback_days: int = 20,
    skip_days: int = 1,
) -> pd.DataFrame:
    """Past-return momentum signal.

    Definition:
        signal_t = (close_{t - skip} / close_{t - skip - lookback}) - 1

    `skip_days=1` excludes the most-recent day so the signal isn't
    contaminated by 1-day reversal (this is the standard adjustment).

    Returns:
        DataFrame [symbol, date, raw_value] - one row per (symbol, date)
        in [start, end] where enough history was available.
    """
    if lookback_days < 1 or skip_days < 0:
        raise ValueError(
            f"Bad params: lookback_days={lookback_days}, skip_days={skip_days}"
        )

    # Pull enough history to compute signal for the earliest `start` date.
    # We need (lookback + skip) trading days BEFORE start; use 2x as a
    # calendar-day buffer to absorb weekends/holidays.
    buffer_calendar = (lookback_days + skip_days) * 2 + 7
    pull_start = start - timedelta(days=buffer_calendar)

    prices = _load_closes(symbols, pull_start, end)
    if prices.empty:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])

    grouped = prices.groupby("symbol", sort=False, group_keys=False)
    skipped = grouped["close"].shift(skip_days)
    lagged = grouped["close"].shift(skip_days + lookback_days)
    signal = (skipped / lagged) - 1.0

    out = pd.DataFrame({
        "symbol": prices["symbol"].values,
        "date": prices["date"].values,
        "raw_value": signal.values,
    })
    out = out[out["raw_value"].notna()].copy()
    out = out[(out["date"] >= start) & (out["date"] <= end)]
    return out.reset_index(drop=True)


def reversal(
    symbols: list[str],
    start: date,
    end: date,
    lookback_days: int = 5,
) -> pd.DataFrame:
    """Short-term reversal: negate the past N-day return.

    Definition:
        signal_t = -((close_t / close_{t - lookback}) - 1)

    A POSITIVE signal means the stock has recently FALLEN, which is the
    "buy the dip" thesis. Standard Phase 1 baseline at lookback_days
    in {3, 5, 10}.

    Returns DataFrame [symbol, date, raw_value].
    """
    if lookback_days < 1:
        raise ValueError(f"lookback_days must be >= 1, got {lookback_days}")

    buffer_calendar = lookback_days * 2 + 7
    pull_start = start - timedelta(days=buffer_calendar)
    prices = _load_closes(symbols, pull_start, end)
    if prices.empty:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])

    grouped = prices.groupby("symbol", sort=False, group_keys=False)
    lagged = grouped["close"].shift(lookback_days)
    signal = -((prices["close"] / lagged) - 1.0)

    out = pd.DataFrame({
        "symbol": prices["symbol"].values,
        "date": prices["date"].values,
        "raw_value": signal.values,
    })
    out = out[out["raw_value"].notna()].copy()
    out = out[(out["date"] >= start) & (out["date"] <= end)]
    return out.reset_index(drop=True)


def volatility(
    symbols: list[str],
    start: date,
    end: date,
    lookback_days: int = 20,
) -> pd.DataFrame:
    """Low-volatility signal: negate the past N-day return stdev.

    Definition:
        ret_t = (close_t / close_{t-1}) - 1
        signal_t = - std(ret over last lookback_days)

    Negation makes "high signal -> low realized vol -> expected
    outperform", matching the low-vol anomaly convention.

    Returns DataFrame [symbol, date, raw_value].
    """
    if lookback_days < 2:
        raise ValueError(f"lookback_days must be >= 2, got {lookback_days}")

    buffer_calendar = lookback_days * 2 + 7
    pull_start = start - timedelta(days=buffer_calendar)
    prices = _load_closes(symbols, pull_start, end)
    if prices.empty:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])

    grouped = prices.groupby("symbol", sort=False, group_keys=False)
    daily_ret = grouped["close"].pct_change()
    # rolling std per symbol, in trading-day positions
    rolling_std = daily_ret.groupby(prices["symbol"], sort=False).transform(
        lambda s: s.rolling(window=lookback_days, min_periods=lookback_days).std(ddof=1)
    )
    signal = -rolling_std

    out = pd.DataFrame({
        "symbol": prices["symbol"].values,
        "date": prices["date"].values,
        "raw_value": signal.values,
    })
    out = out[out["raw_value"].notna()].copy()
    out = out[(out["date"] >= start) & (out["date"] <= end)]
    return out.reset_index(drop=True)


def _load_closes(
    symbols: list[str],
    start: date,
    end: date,
) -> pd.DataFrame:
    """Pull (symbol, date, close) for [start, end]. Sorted, NaN-cleaned."""
    if not symbols:
        return pd.DataFrame(columns=["symbol", "date", "close"])
    sql = text("""
        SELECT symbol, date, close
          FROM daily_prices
         WHERE symbol = ANY(:syms)
           AND date BETWEEN :s AND :e
           AND close IS NOT NULL
           AND close > 0
         ORDER BY symbol, date
    """)
    engine = get_engine()
    with engine.connect() as conn:
        df = pd.read_sql(
            sql, conn,
            params={"syms": symbols, "s": start, "e": end},
        )
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df


# Registry mapping factor_name string -> (function, kwargs).
# Used by factor_eval to look up baseline factors by name from CLI.
BASELINE_FACTORS: dict[str, dict] = {
    "momentum_20d": {"fn": momentum, "kwargs": {"lookback_days": 20, "skip_days": 1}},
    "momentum_60d": {"fn": momentum, "kwargs": {"lookback_days": 60, "skip_days": 1}},
    "momentum_120d": {"fn": momentum, "kwargs": {"lookback_days": 120, "skip_days": 1}},
    "reversal_5d": {"fn": reversal, "kwargs": {"lookback_days": 5}},
    "reversal_10d": {"fn": reversal, "kwargs": {"lookback_days": 10}},
    "volatility_20d": {"fn": volatility, "kwargs": {"lookback_days": 20}},
}
