"""Earnings revision factors from FnGuide consensus estimates.

Theory (Stickel 1991; Chan/Jegadeesh/Lakonishok 1996; decades of replication):
    When analysts collectively raise their forward EPS estimate for a
    stock, that stock tends to outperform over the subsequent 1-3 months.
    This is one of the most robust anomalies in equity factor research.

Point-in-time strategy:
    consensus_estimates.as_of_date is the SNAPSHOT date (the day the
    estimate was visible). On evaluation date t we look at:
        - the latest as_of_date <= t  (current estimate)
        - the latest as_of_date <= t - lookback_days  (past estimate)
    Then signal = (current - past) / |past|.

    This is naturally point-in-time because as_of_date is the data's
    own timestamp - no need to join with disclosure-date metadata.

Fiscal period selection:
    For each signal, we pick a target fiscal_period. The default is
    'FY+1' (the next full fiscal year), which is the most-watched
    forward estimate in practice. Some signals also use 'FQ+1'
    (next quarter) for shorter-horizon strategies.

    fiscal_period strings are like 'FY2026' or '2026Q3'. We map
    "FY+1 relative to t" -> "the smallest FY{year} where year > t.year".

Factors implemented:
    rev_eps_1m_fy1   - 1-month revision of FY+1 EPS estimate
    rev_eps_3m_fy1   - 3-month revision of FY+1 EPS estimate
    rev_eps_1m_fq1   - 1-month revision of next quarter EPS estimate

Output (matches BASELINE_FACTORS shape):
    DataFrame [symbol, date, raw_value], one row per (symbol, date)
    where the revision is computable.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text

from src.db.connection import get_engine

# Match 'FY2026' or '2026Q3'
_FY_RE = re.compile(r"^FY(\d{4})$")
_FQ_RE = re.compile(r"^(\d{4})Q(\d)$")


def _select_target_period(
    fiscal_periods: pd.Series,
    base_date: date,
    target_type: str = "FY+1",
) -> str | None:
    """Pick the right fiscal_period string for `base_date`.

    target_type:
        'FY+1' : smallest FY year > base_date.year
        'FQ+1' : smallest YYYYQn after base_date's calendar quarter
    """
    if target_type == "FY+1":
        candidates = []
        for p in fiscal_periods.dropna().unique():
            m = _FY_RE.match(str(p))
            if m:
                year = int(m.group(1))
                if year > base_date.year:
                    candidates.append((year, p))
        if not candidates:
            return None
        return min(candidates, key=lambda x: x[0])[1]

    if target_type == "FQ+1":
        # current quarter of base_date
        base_q = (base_date.month - 1) // 3 + 1
        candidates = []
        for p in fiscal_periods.dropna().unique():
            m = _FQ_RE.match(str(p))
            if m:
                year, q = int(m.group(1)), int(m.group(2))
                # period is "after" base date if year > base.year, OR
                # same year and q > base_q
                if year > base_date.year or (year == base_date.year and q > base_q):
                    candidates.append((year, q, p))
        if not candidates:
            return None
        return min(candidates, key=lambda x: (x[0], x[1]))[2]

    raise ValueError(f"Unknown target_type: {target_type}")


def _load_eps_snapshots(
    symbols: list[str],
    start: date,
    end: date,
    fiscal_period_type: str | None = None,
) -> pd.DataFrame:
    """Load consensus_estimates for the symbols/range.

    Returns columns: symbol, as_of_date, fiscal_period, fiscal_period_type,
    eps_estimate. Filters out NULL EPS rows. Optionally restricts to
    'annual' or 'quarterly' periods.

    Note on range: we pull data starting `start - lookback_buffer` so the
    "past estimate" can be found for early rows. The caller passes a
    pre-buffered start.
    """
    if not symbols:
        return pd.DataFrame()

    where = [
        "symbol = ANY(:syms)",
        "as_of_date BETWEEN :s AND :e",
        "eps_estimate IS NOT NULL",
    ]
    params: dict = {"syms": symbols, "s": start, "e": end}
    if fiscal_period_type is not None:
        where.append("fiscal_period_type = :ft")
        params["ft"] = fiscal_period_type

    sql = text(
        "SELECT symbol, as_of_date, fiscal_period, fiscal_period_type, eps_estimate "
        "FROM consensus_estimates "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY symbol, fiscal_period, as_of_date"
    )
    with get_engine().connect() as conn:
        df = pd.read_sql(sql, conn, params=params)

    if df.empty:
        return df
    df["as_of_date"] = pd.to_datetime(df["as_of_date"]).dt.date
    df["eps_estimate"] = pd.to_numeric(df["eps_estimate"], errors="coerce")
    return df


def _trading_days_in_range(
    start: date,
    end: date,
) -> list[date]:
    """Business days in [start, end]. Holiday filtering happens later
    via inner-join with daily_prices."""
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _filter_to_trading_days(
    df: pd.DataFrame,
    symbols: list[str],
    start: date,
    end: date,
) -> pd.DataFrame:
    """Keep only (symbol, date) pairs that have daily_prices rows.

    Without this, signals computed on weekends/holidays produce phantom
    rows that the cross-sectional evaluation later has to drop. Doing
    it here keeps the output clean.
    """
    if df.empty:
        return df
    sql = text("""
        SELECT symbol, date FROM daily_prices
         WHERE symbol = ANY(:syms)
           AND date BETWEEN :s AND :e
           AND close IS NOT NULL
           AND close > 0
    """)
    with get_engine().connect() as conn:
        valid = pd.read_sql(
            sql, conn,
            params={"syms": symbols, "s": start, "e": end},
        )
    if valid.empty:
        return pd.DataFrame(columns=df.columns)
    valid["date"] = pd.to_datetime(valid["date"]).dt.date
    return df.merge(valid, on=["symbol", "date"], how="inner")


def _compute_revision_signal(
    snapshots: pd.DataFrame,
    symbols: list[str],
    start: date,
    end: date,
    lookback_days: int,
    target_type: str,
) -> pd.DataFrame:
    """Core revision computation.

    For each (symbol, evaluation date t) in business days [start, end]:
        - pick target fiscal_period via target_type
        - find latest eps_estimate with as_of_date <= t
        - find latest eps_estimate with as_of_date <= t - lookback_days
        - signal = (current - past) / max(|past|, 1)

    The denominator uses max(|past|, 1) to avoid extreme blowups when
    past EPS is near zero (typical for loss-making companies).
    Eyeballed: this matches the 'revision percentage' convention used
    in the academic literature without exploding.

    Returns DataFrame [symbol, date, raw_value].
    """
    if snapshots.empty:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])

    # Per-symbol processing
    out_rows: list[dict] = []
    trading_days = _trading_days_in_range(start, end)

    for sym, sym_df in snapshots.groupby("symbol", sort=False):
        # Choose target fiscal_period once per (symbol, t) since it can
        # change as years roll. To keep this fast, compute once per
        # calendar year boundary inside the loop.
        sym_df = sym_df.sort_values("as_of_date").reset_index(drop=True)
        as_of_arr = sym_df["as_of_date"].values
        fp_arr = sym_df["fiscal_period"].values
        eps_arr = sym_df["eps_estimate"].values

        for t in trading_days:
            tgt_period = _select_target_period(
                pd.Series(fp_arr), t, target_type=target_type
            )
            if tgt_period is None:
                continue

            past_cutoff = t - timedelta(days=lookback_days)

            # Find current = latest row with as_of_date <= t and fp == tgt
            current_eps: float | None = None
            past_eps: float | None = None
            for i in range(len(sym_df) - 1, -1, -1):
                if fp_arr[i] != tgt_period:
                    continue
                if as_of_arr[i] > t:
                    continue
                if current_eps is None:
                    current_eps = float(eps_arr[i])
                if as_of_arr[i] <= past_cutoff:
                    past_eps = float(eps_arr[i])
                    break

            if current_eps is None or past_eps is None:
                continue

            denom = max(abs(past_eps), 1.0)
            signal = (current_eps - past_eps) / denom
            out_rows.append({
                "symbol": sym,
                "date": t,
                "raw_value": signal,
            })

    if not out_rows:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])
    out = pd.DataFrame(out_rows)
    return _filter_to_trading_days(out, symbols, start, end)


def revision_eps(
    symbols: list[str],
    start: date,
    end: date,
    lookback_days: int = 30,
    target_type: str = "FY+1",
) -> pd.DataFrame:
    """Generic EPS revision factor.

    Args:
        symbols: 6-digit KRX symbols.
        start, end: evaluation date range (base dates t).
        lookback_days: how far back to pull the "past estimate". 30 for
            1-month revision, 90 for 3-month.
        target_type: 'FY+1' (next fiscal year) or 'FQ+1' (next quarter).
            FY+1 is the standard choice in academic literature.

    Returns DataFrame [symbol, date, raw_value] following the
    BASELINE_FACTORS contract.
    """
    if lookback_days < 1:
        raise ValueError(f"lookback_days must be >= 1, got {lookback_days}")

    # Pull buffered data so the past-estimate lookup works for early dates
    pull_start = start - timedelta(days=lookback_days + 14)
    period_type = "annual" if target_type == "FY+1" else "quarterly"
    snapshots = _load_eps_snapshots(
        symbols, pull_start, end, fiscal_period_type=period_type
    )
    if snapshots.empty:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])

    return _compute_revision_signal(
        snapshots, symbols, start, end,
        lookback_days=lookback_days, target_type=target_type,
    )


# Registry entries to be merged into BASELINE_FACTORS at import time
# (factor_eval handles this via the FACTORS module-level dict).
REVISION_FACTORS: dict[str, dict] = {
    "rev_eps_1m_fy1": {
        "fn": revision_eps,
        "kwargs": {"lookback_days": 30, "target_type": "FY+1"},
    },
    "rev_eps_3m_fy1": {
        "fn": revision_eps,
        "kwargs": {"lookback_days": 90, "target_type": "FY+1"},
    },
    "rev_eps_1m_fq1": {
        "fn": revision_eps,
        "kwargs": {"lookback_days": 30, "target_type": "FQ+1"},
    },
}
