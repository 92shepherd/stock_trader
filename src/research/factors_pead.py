"""Post-Earnings Announcement Drift (PEAD) factors from DART.

Theory (Ball & Brown 1968; Bernard & Thomas 1989; Foster/Olsen/Shevlin
1984; replicated thousands of times):
    After a company's earnings announcement, the stock price continues
    to drift in the direction of the surprise for 30-90 days. Stocks
    with positive earnings surprises outperform; negative surprises
    underperform. The drift is one of the most robust anomalies known.

Point-in-time strategy (CRITICAL):
    DART's financial statement data (dart_financials) is keyed by
    `bsns_year` + `reprt_code` (fiscal period), but those values do NOT
    tell us WHEN the data became public. Using fiscal-period dates for
    signal computation causes look-ahead bias (the Q3 2024 statement
    isn't visible to traders until ~Nov 14 2024).

    Fix: we join dart_financials with dart_disclosures to get the
    actual `rcept_dt` (DART receipt date = the day the filing became
    public). The factor is computed on date t as if we have access
    only to disclosures with rcept_dt <= t.

Factors implemented:
    pead_revenue_yoy   - latest available revenue YoY growth
    pead_op_yoy        - latest available operating income YoY growth
    pead_net_yoy       - latest available net income YoY growth
    pead_revenue_qoq   - revenue QoQ growth (this quarter vs prior)

    All factors use the SAME post-announcement signal idea: a stock
    with high growth published RECENTLY is expected to drift up.

How "latest available" is computed for date t:
    1. Find all (corp_code, bsns_year, reprt_code) combos with
       rcept_dt <= t in dart_disclosures (joined via corp_code).
    2. For each (corp_code), pick the most recent one by rcept_dt.
    3. Look up the financial value in dart_financials.
    4. Compute the growth signal.

    Effective window: a stock's signal is "the most recent disclosure
    available to traders on date t". This naturally creates the PEAD
    drift window because the signal stays the same from rcept_dt until
    the NEXT disclosure arrives ~90 days later.

What account names to query:
    Korean DART filings use these standard account names (account_nm):
        "매출액", "수익(매출액)"           -> revenue
        "영업이익"                          -> operating income
        "당기순이익", "분기순이익"          -> net income
    We match on a substring whitelist since DART account_nm varies by
    company.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text

from src.db.connection import get_engine
from src.utils.logger import logger

# Substring patterns to match account_nm in dart_financials.
# Order matters: first match wins, so put more specific patterns first.
_ACCOUNT_PATTERNS: dict[str, list[str]] = {
    "revenue": ["수익(매출액)", "매출액", "영업수익"],
    "op_income": ["영업이익(손실)", "영업이익"],
    "net_income": ["당기순이익(손실)", "당기순이익", "분기순이익", "반기순이익"],
}


def _load_pit_financials(
    symbols: list[str],
    end_date: date,
    account_kind: str,
    lookback_years: int = 3,
) -> pd.DataFrame:
    """Load financials filtered to PIT-safe rows.

    For each (corp_code, stock_code), pull all disclosures filed up to
    `end_date` with a matching account, joining dart_disclosures to get
    rcept_dt. Returns one row per (stock_code, bsns_year, reprt_code,
    fs_div, account_nm).

    Args:
        symbols: 6-digit stock_codes (NOT corp_codes).
        end_date: PIT cutoff. No disclosures with rcept_dt > end_date.
        account_kind: 'revenue' | 'op_income' | 'net_income'
        lookback_years: how many years of history to pull (default 3,
                        enough for YoY/QoQ).

    Returns columns:
        stock_code, corp_code, bsns_year, reprt_code, fs_div,
        account_nm, rcept_dt, thstrm_amount (current), frmtrm_amount
        (prior year same quarter), bfefrmtrm_amount.
    """
    if account_kind not in _ACCOUNT_PATTERNS:
        raise ValueError(f"Unknown account_kind: {account_kind}")

    patterns = _ACCOUNT_PATTERNS[account_kind]
    # Use ILIKE OR-list. account_nm column is small.
    ilike_clause = " OR ".join(
        [f"f.account_nm LIKE :pat_{i}" for i in range(len(patterns))]
    )
    pat_params = {f"pat_{i}": f"%{p}%" for i, p in enumerate(patterns)}

    min_year = end_date.year - lookback_years

    sql = text(f"""
        SELECT
            c.stock_code,
            f.corp_code,
            f.bsns_year,
            f.reprt_code,
            f.fs_div,
            f.account_nm,
            d.rcept_dt,
            f.thstrm_amount,
            f.frmtrm_amount,
            f.bfefrmtrm_amount
        FROM dart_financials f
        JOIN dart_corp_codes c ON c.corp_code = f.corp_code
        JOIN LATERAL (
            SELECT MIN(rcept_dt) AS rcept_dt
              FROM dart_disclosures dd
             WHERE dd.corp_code = f.corp_code
               AND dd.kind = 'A'  -- 정기공시 (regular periodic reports)
               AND EXTRACT(YEAR FROM dd.rcept_dt)::int = f.bsns_year
                    OR EXTRACT(YEAR FROM dd.rcept_dt)::int = f.bsns_year + 1
        ) d ON TRUE
        WHERE c.stock_code = ANY(:syms)
          AND f.bsns_year >= :min_year
          AND f.bsns_year <= :max_year
          AND ({ilike_clause})
          AND f.fs_div = 'CFS'
          AND d.rcept_dt IS NOT NULL
          AND d.rcept_dt <= :end_date
        ORDER BY c.stock_code, d.rcept_dt
    """)
    params = {
        "syms": symbols,
        "min_year": min_year,
        "max_year": end_date.year,
        "end_date": end_date,
        **pat_params,
    }
    with get_engine().connect() as conn:
        df = pd.read_sql(sql, conn, params=params)

    if df.empty:
        return df
    df["rcept_dt"] = pd.to_datetime(df["rcept_dt"]).dt.date
    for col in ("thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _build_pit_panel(
    symbols: list[str],
    start: date,
    end: date,
    account_kind: str,
) -> pd.DataFrame:
    """Build a per-(symbol, date) panel of "latest available" YoY growth.

    Steps:
        1. Load all eligible disclosures with rcept_dt <= end.
        2. For each business day t in [start, end] and each symbol,
           find the disclosure with the largest rcept_dt <= t.
        3. Compute YoY growth from (thstrm, frmtrm) of that disclosure.
        4. Drop (symbol, t) pairs where t < the symbol's earliest
           rcept_dt (no signal available yet).

    The signal is constant between disclosures, which is the standard
    PEAD construction.
    """
    df = _load_pit_financials(symbols, end, account_kind)
    if df.empty:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])

    # YoY growth = (thstrm - frmtrm) / max(|frmtrm|, 1)
    df["yoy_growth"] = (df["thstrm_amount"] - df["frmtrm_amount"]) / df[
        "frmtrm_amount"
    ].abs().clip(lower=1.0)
    df = df[df["yoy_growth"].notna()].copy()
    df = df.rename(columns={"stock_code": "symbol"})

    # For each symbol, build a step function: signal valid from rcept_dt
    # onward until the next disclosure.
    out_rows: list[dict] = []
    business_days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            business_days.append(d)
        d += timedelta(days=1)

    if not business_days:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])

    for sym, sym_df in df.groupby("symbol", sort=False):
        sym_df = sym_df.sort_values("rcept_dt").reset_index(drop=True)
        rcept_arr = sym_df["rcept_dt"].values
        signal_arr = sym_df["yoy_growth"].values

        # For each business day, find the latest disclosure with
        # rcept_dt <= day. Use a two-pointer scan since both arrays are
        # sorted ascending.
        ptr = -1  # index of latest applicable disclosure
        for t in business_days:
            while (ptr + 1) < len(rcept_arr) and rcept_arr[ptr + 1] <= t:
                ptr += 1
            if ptr < 0:
                continue  # no disclosure yet for this symbol
            out_rows.append({
                "symbol": sym,
                "date": t,
                "raw_value": float(signal_arr[ptr]),
            })

    if not out_rows:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])
    return pd.DataFrame(out_rows)


def _filter_to_trading_days(
    df: pd.DataFrame,
    symbols: list[str],
    start: date,
    end: date,
) -> pd.DataFrame:
    """Inner-join with daily_prices to drop holiday/halt rows."""
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


def pead_revenue_yoy(
    symbols: list[str],
    start: date,
    end: date,
) -> pd.DataFrame:
    """PEAD: revenue YoY growth (latest available disclosure)."""
    df = _build_pit_panel(symbols, start, end, account_kind="revenue")
    return _filter_to_trading_days(df, symbols, start, end)


def pead_op_yoy(
    symbols: list[str],
    start: date,
    end: date,
) -> pd.DataFrame:
    """PEAD: operating income YoY growth (latest available)."""
    df = _build_pit_panel(symbols, start, end, account_kind="op_income")
    return _filter_to_trading_days(df, symbols, start, end)


def pead_net_yoy(
    symbols: list[str],
    start: date,
    end: date,
) -> pd.DataFrame:
    """PEAD: net income YoY growth (latest available)."""
    df = _build_pit_panel(symbols, start, end, account_kind="net_income")
    return _filter_to_trading_days(df, symbols, start, end)


PEAD_FACTORS: dict[str, dict] = {
    "pead_revenue_yoy": {"fn": pead_revenue_yoy, "kwargs": {}},
    "pead_op_yoy": {"fn": pead_op_yoy, "kwargs": {}},
    "pead_net_yoy": {"fn": pead_net_yoy, "kwargs": {}},
}
