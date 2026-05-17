"""Analyst rating / target price factors from 한경 컨센서스 데이터.

Theory (Womack 1996; many replications):
    Stocks that receive analyst rating upgrades (e.g., Hold -> Buy)
    show positive short-term abnormal returns; downgrades show
    negative returns. The CHANGE matters more than the level.

    Separately, the average target price implied "upside" (target /
    current_price - 1) carries information about analyst consensus on
    valuation. Higher upside = more bullish coverage.

Point-in-time strategy:
    hankyung_reports.report_date is when the report became public.
    No additional join needed - the date is already PIT-safe.

Factors implemented:
    rating_upgrade_30d - count of upgrades in last 30 days minus count
                         of downgrades. Positive value = net positive
                         momentum from analysts.
    rating_target_upside - latest avg target_price / current close - 1
                           (using last 60 days of reports for the avg).
    rating_coverage - number of distinct analysts covering the stock
                      in the last 90 days. Coverage momentum is a
                      well-known signal.

Rating change detection:
    grade_value field has free-form text ("Buy", "Strong Buy", "Hold",
    "매수", "강력매수", etc). We normalize to a numeric scale:
        Strong Buy / 적극매수 / 강력매수      -> 5
        Buy / 매수                            -> 4
        Outperform / Overweight / 비중확대    -> 4
        Hold / Neutral / 중립                 -> 3
        Underperform / Underweight / 비중축소 -> 2
        Sell / 매도                           -> 1
        Strong Sell                           -> 0
    Then an "upgrade" is a report whose normalized rating is > the
    previous report from the SAME (office_name, business_code) pair.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text

from src.db.connection import get_engine

# Rating normalization map. Lowercase, stripped.
_RATING_MAP: dict[str, int] = {
    # English
    "strong buy": 5,
    "buy": 4,
    "outperform": 4,
    "overweight": 4,
    "accumulate": 4,
    "add": 4,
    "hold": 3,
    "neutral": 3,
    "market perform": 3,
    "marketperform": 3,
    "reduce": 2,
    "underweight": 2,
    "underperform": 2,
    "sell": 1,
    "strong sell": 0,
    # Korean
    "적극매수": 5,
    "강력매수": 5,
    "매수": 4,
    "비중확대": 4,
    "보유": 3,
    "중립": 3,
    "비중축소": 2,
    "축소": 2,
    "매도": 1,
}


def _normalize_rating(text_val: str | None) -> int | None:
    """Normalize a free-form rating string to 0-5 integer. None if unknown."""
    if not text_val:
        return None
    s = str(text_val).strip().lower()
    # Direct lookup
    if s in _RATING_MAP:
        return _RATING_MAP[s]
    # Try substring matches (catch "Buy (유지)" etc)
    for key, val in _RATING_MAP.items():
        if key in s:
            return val
    return None


def _load_reports(
    symbols: list[str],
    start: date,
    end: date,
) -> pd.DataFrame:
    """Pull hankyung_reports for the given symbols/date range.

    Returns columns: symbol(business_code), report_date, rating_norm,
    target_price, analyst, office_name.
    """
    if not symbols:
        return pd.DataFrame()
    sql = text("""
        SELECT
            business_code  AS symbol,
            report_date,
            grade_value,
            target_price,
            analyst,
            office_name
          FROM hankyung_reports
         WHERE business_code = ANY(:syms)
           AND report_date BETWEEN :s AND :e
           AND business_code IS NOT NULL
         ORDER BY business_code, report_date
    """)
    with get_engine().connect() as conn:
        df = pd.read_sql(
            sql, conn,
            params={"syms": symbols, "s": start, "e": end},
        )
    if df.empty:
        return df
    df["report_date"] = pd.to_datetime(df["report_date"]).dt.date
    df["rating_norm"] = df["grade_value"].apply(_normalize_rating)
    df["target_price"] = pd.to_numeric(df["target_price"], errors="coerce")
    return df


def _detect_rating_changes(reports: pd.DataFrame) -> pd.DataFrame:
    """For each (symbol, office, report_date), determine if it was an
    upgrade (+1), downgrade (-1), or unchanged (0) relative to the
    PREVIOUS report from the same (symbol, office).

    Returns a new DataFrame with a `delta` column added.
    """
    if reports.empty:
        return reports
    df = reports.copy()
    df = df.sort_values(["symbol", "office_name", "report_date"])
    df["prev_rating"] = df.groupby(["symbol", "office_name"])["rating_norm"].shift(1)
    # delta in [-5, +5], or NaN when there's no prior report
    df["delta"] = df["rating_norm"] - df["prev_rating"]
    return df


def _trading_days(start: date, end: date) -> list[date]:
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
    if df.empty:
        return df
    sql = text("""
        SELECT symbol, date FROM daily_prices
         WHERE symbol = ANY(:syms)
           AND date BETWEEN :s AND :e
           AND close IS NOT NULL AND close > 0
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


def rating_upgrade_30d(
    symbols: list[str],
    start: date,
    end: date,
    lookback_days: int = 30,
) -> pd.DataFrame:
    """Net upgrade count in last `lookback_days`.

    Signal = (number of upgrades) - (number of downgrades) in window.
    Reports with no prior rating (first coverage) are ignored.

    Returns DataFrame [symbol, date, raw_value].
    """
    # Pull a buffer so the lookback works at start date
    pull_start = start - timedelta(days=lookback_days + 7)
    reports = _load_reports(symbols, pull_start, end)
    if reports.empty:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])

    reports = _detect_rating_changes(reports)
    reports = reports[reports["delta"].notna()].copy()
    # Classify each report as +1 (upgrade), -1 (downgrade), 0 otherwise
    reports["change_sign"] = reports["delta"].apply(
        lambda x: 1 if x > 0 else (-1 if x < 0 else 0)
    )
    # Keep only true changes
    reports = reports[reports["change_sign"] != 0]
    if reports.empty:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])

    # For each (symbol, t), count change_sign in [t - lookback, t]
    out_rows: list[dict] = []
    business_days = _trading_days(start, end)
    for sym, sym_df in reports.groupby("symbol", sort=False):
        sym_df = sym_df.sort_values("report_date")
        dates_arr = sym_df["report_date"].values
        signs_arr = sym_df["change_sign"].values

        for t in business_days:
            window_start = t - timedelta(days=lookback_days)
            mask = (dates_arr >= window_start) & (dates_arr <= t)
            if not mask.any():
                continue
            score = int(signs_arr[mask].sum())
            out_rows.append({"symbol": sym, "date": t, "raw_value": float(score)})

    if not out_rows:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])
    out = pd.DataFrame(out_rows)
    return _filter_to_trading_days(out, symbols, start, end)


def rating_target_upside(
    symbols: list[str],
    start: date,
    end: date,
    lookback_days: int = 60,
) -> pd.DataFrame:
    """Average target price implied upside.

    For each (symbol, t):
        avg_target = mean(target_price) over reports in [t-lookback, t]
        current_close = daily_prices.close on t
        signal = avg_target / current_close - 1

    A positive value means analysts collectively see upside relative to
    the current price. Returns DataFrame [symbol, date, raw_value].
    """
    pull_start = start - timedelta(days=lookback_days + 7)
    reports = _load_reports(symbols, pull_start, end)
    if reports.empty:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])
    reports = reports[reports["target_price"].notna() & (reports["target_price"] > 0)]
    if reports.empty:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])

    # Pull close prices for the eval window
    sql = text("""
        SELECT symbol, date, close
          FROM daily_prices
         WHERE symbol = ANY(:syms)
           AND date BETWEEN :s AND :e
           AND close IS NOT NULL AND close > 0
    """)
    with get_engine().connect() as conn:
        closes = pd.read_sql(
            sql, conn,
            params={"syms": symbols, "s": start, "e": end},
        )
    if closes.empty:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])
    closes["date"] = pd.to_datetime(closes["date"]).dt.date
    closes["close"] = pd.to_numeric(closes["close"], errors="coerce")

    out_rows: list[dict] = []
    for sym, sym_reports in reports.groupby("symbol", sort=False):
        sym_reports = sym_reports.sort_values("report_date")
        dates_arr = sym_reports["report_date"].values
        tp_arr = sym_reports["target_price"].values

        sym_closes = closes[closes["symbol"] == sym]
        for _, crow in sym_closes.iterrows():
            t = crow["date"]
            c = float(crow["close"])
            if c <= 0:
                continue
            window_start = t - timedelta(days=lookback_days)
            mask = (dates_arr >= window_start) & (dates_arr <= t)
            if not mask.any():
                continue
            avg_target = float(tp_arr[mask].mean())
            out_rows.append({
                "symbol": sym,
                "date": t,
                "raw_value": (avg_target / c) - 1.0,
            })

    if not out_rows:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])
    return pd.DataFrame(out_rows)


def rating_coverage(
    symbols: list[str],
    start: date,
    end: date,
    lookback_days: int = 90,
) -> pd.DataFrame:
    """Distinct analyst coverage count in last `lookback_days`.

    Signal = number of distinct (office_name) producing a report in
    the window. Higher coverage tends to correlate with positive
    expectations.

    Returns DataFrame [symbol, date, raw_value].
    """
    pull_start = start - timedelta(days=lookback_days + 7)
    reports = _load_reports(symbols, pull_start, end)
    if reports.empty:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])

    out_rows: list[dict] = []
    business_days = _trading_days(start, end)
    for sym, sym_df in reports.groupby("symbol", sort=False):
        sym_df = sym_df.sort_values("report_date")
        for t in business_days:
            window_start = t - timedelta(days=lookback_days)
            mask = (
                (sym_df["report_date"] >= window_start)
                & (sym_df["report_date"] <= t)
            )
            window = sym_df[mask]
            if window.empty:
                continue
            n_offices = window["office_name"].nunique()
            out_rows.append({
                "symbol": sym, "date": t, "raw_value": float(n_offices),
            })

    if not out_rows:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])
    out = pd.DataFrame(out_rows)
    return _filter_to_trading_days(out, symbols, start, end)


RATING_FACTORS: dict[str, dict] = {
    "rating_upgrade_30d": {
        "fn": rating_upgrade_30d,
        "kwargs": {"lookback_days": 30},
    },
    "rating_target_upside": {
        "fn": rating_target_upside,
        "kwargs": {"lookback_days": 60},
    },
    "rating_coverage_90d": {
        "fn": rating_coverage,
        "kwargs": {"lookback_days": 90},
    },
}
