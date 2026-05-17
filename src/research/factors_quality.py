"""Quality factors from DART financial indicators.

Theory (Novy-Marx 2013; Asness/Frazzini/Pedersen 2019; Sloan 1996):
    Companies with high profitability (gross/operating margins, ROE,
    ROA) and stable balance sheets tend to outperform low-quality
    peers, especially over multi-month horizons. The "quality"
    anomaly is one of the strongest in international evidence.

Data source:
    dart_indicators has DART-precomputed ratios under idx_cl_code
    categories:
        M210000 - 수익성 (Profitability): ROA, ROE, op margin, ...
        M220000 - 안정성 (Stability): debt ratio, current ratio, ...
        M230000 - 성장성 (Growth): revenue growth, ...
        M240000 - 활동성 (Activity): asset turnover, ...

    Using DART's precomputed ratios saves us from accounting-policy
    edge cases (consolidated vs separate, IFRS treatment, etc).

Point-in-time strategy (same as PEAD):
    dart_indicators is keyed by (corp_code, bsns_year, reprt_code,
    fs_div, idx_nm) - no rcept_dt. We must join dart_disclosures by
    corp_code to get the actual publication date.

Factors implemented:
    quality_roe       - latest available ROE
    quality_op_margin - latest available operating margin
    quality_debt_ratio - inverted: lower debt ratio = higher signal
    quality_roa       - latest available ROA

idx_nm matching:
    DART's indicator names in Korean:
        "ROE(자기자본순이익률)"        -> ROE
        "ROA(총자산순이익률)"          -> ROA
        "영업이익률"                   -> operating margin
        "부채비율"                     -> debt ratio
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text

from src.db.connection import get_engine

# Mapping of factor kind -> list of idx_nm substring patterns to match.
# Order matters; first match wins.
_INDICATOR_PATTERNS: dict[str, dict] = {
    "roe": {
        "patterns": ["ROE", "자기자본순이익률"],
        "invert": False,
    },
    "roa": {
        "patterns": ["ROA", "총자산순이익률"],
        "invert": False,
    },
    "op_margin": {
        "patterns": ["영업이익률"],
        "invert": False,
    },
    "debt_ratio": {
        "patterns": ["부채비율"],
        "invert": True,  # lower is better
    },
}


def _load_pit_indicators(
    symbols: list[str],
    end_date: date,
    factor_kind: str,
    lookback_years: int = 2,
) -> pd.DataFrame:
    """Load PIT-safe indicator rows.

    For each (corp_code, stock_code, indicator) join dart_disclosures
    via corp_code to get the regular-report (kind='A') filing date.

    Returns columns:
        stock_code, corp_code, bsns_year, reprt_code, idx_nm,
        rcept_dt, thstrm_value.
    """
    if factor_kind not in _INDICATOR_PATTERNS:
        raise ValueError(f"Unknown factor_kind: {factor_kind}")

    patterns = _INDICATOR_PATTERNS[factor_kind]["patterns"]
    ilike_clause = " OR ".join(
        [f"i.idx_nm LIKE :pat_{j}" for j in range(len(patterns))]
    )
    pat_params = {f"pat_{j}": f"%{p}%" for j, p in enumerate(patterns)}

    min_year = end_date.year - lookback_years

    sql = text(f"""
        SELECT
            c.stock_code,
            i.corp_code,
            i.bsns_year,
            i.reprt_code,
            i.idx_nm,
            d.rcept_dt,
            i.thstrm_value
          FROM dart_indicators i
          JOIN dart_corp_codes c ON c.corp_code = i.corp_code
          JOIN LATERAL (
                SELECT MIN(rcept_dt) AS rcept_dt
                  FROM dart_disclosures dd
                 WHERE dd.corp_code = i.corp_code
                   AND dd.kind = 'A'
                   AND (
                        EXTRACT(YEAR FROM dd.rcept_dt)::int = i.bsns_year
                     OR EXTRACT(YEAR FROM dd.rcept_dt)::int = i.bsns_year + 1
                   )
          ) d ON TRUE
         WHERE c.stock_code = ANY(:syms)
           AND i.bsns_year >= :min_year
           AND i.bsns_year <= :max_year
           AND ({ilike_clause})
           AND i.fs_div = 'CFS'
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
    df["thstrm_value"] = pd.to_numeric(df["thstrm_value"], errors="coerce")
    return df


def _build_pit_panel(
    symbols: list[str],
    start: date,
    end: date,
    factor_kind: str,
) -> pd.DataFrame:
    """For each (symbol, business day t), report the latest available
    indicator value with rcept_dt <= t.

    Same pattern as factors_pead._build_pit_panel.
    """
    df = _load_pit_indicators(symbols, end, factor_kind)
    if df.empty:
        return pd.DataFrame(columns=["symbol", "date", "raw_value"])

    invert = _INDICATOR_PATTERNS[factor_kind]["invert"]
    df = df.rename(columns={"stock_code": "symbol"})
    df = df[df["thstrm_value"].notna()].copy()
    if invert:
        df["thstrm_value"] = -df["thstrm_value"]

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
        val_arr = sym_df["thstrm_value"].values

        ptr = -1
        for t in business_days:
            while (ptr + 1) < len(rcept_arr) and rcept_arr[ptr + 1] <= t:
                ptr += 1
            if ptr < 0:
                continue
            out_rows.append({
                "symbol": sym,
                "date": t,
                "raw_value": float(val_arr[ptr]),
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


def quality_roe(
    symbols: list[str], start: date, end: date,
) -> pd.DataFrame:
    df = _build_pit_panel(symbols, start, end, factor_kind="roe")
    return _filter_to_trading_days(df, symbols, start, end)


def quality_roa(
    symbols: list[str], start: date, end: date,
) -> pd.DataFrame:
    df = _build_pit_panel(symbols, start, end, factor_kind="roa")
    return _filter_to_trading_days(df, symbols, start, end)


def quality_op_margin(
    symbols: list[str], start: date, end: date,
) -> pd.DataFrame:
    df = _build_pit_panel(symbols, start, end, factor_kind="op_margin")
    return _filter_to_trading_days(df, symbols, start, end)


def quality_debt_ratio(
    symbols: list[str], start: date, end: date,
) -> pd.DataFrame:
    """Inverted debt ratio: positive = LOW leverage = good quality."""
    df = _build_pit_panel(symbols, start, end, factor_kind="debt_ratio")
    return _filter_to_trading_days(df, symbols, start, end)


QUALITY_FACTORS: dict[str, dict] = {
    "quality_roe": {"fn": quality_roe, "kwargs": {}},
    "quality_roa": {"fn": quality_roa, "kwargs": {}},
    "quality_op_margin": {"fn": quality_op_margin, "kwargs": {}},
    "quality_debt_ratio_inv": {"fn": quality_debt_ratio, "kwargs": {}},
}
