"""Load a wide-format factor panel from factor_signals.

This module is the only place where bots read factor_signals. Returning
a wide DataFrame is the entire reason this layer exists — the bot
engine then picks columns by name without re-hitting the DB.

Output shape:
    pd.DataFrame with columns:
        symbol, sector, market_cap, avg_value_20d,
        <factor_1>, <factor_2>, ...
    one row per symbol in the universe-on-`as_of_date`.

`<factor_N>` column values come from `factor_source`:
    'rank_value'   - cross-sectional percentile rank in [0, 1] (default)
    'z_score'      - cross-sectional z-score
    'neutral_value'- sector-neutralized raw value
    'raw_value'    - raw signal (not recommended for combining)
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text

from src.db.connection import get_engine
from src.utils.logger import logger


_VALID_SOURCES = ("rank_value", "z_score", "neutral_value", "raw_value")


def load_factor_panel(
    factors: list[str],
    universe: str,
    as_of: date,
    *,
    factor_source: str = "rank_value",
    include_market_cap: bool = True,
    include_avg_value: bool = True,
    avg_value_lookback_days: int = 20,
) -> pd.DataFrame:
    """Load wide-format factor panel for one decision date.

    For (symbol, as_of) in the factor_signals where factor_name is in
    `factors`, pivot the chosen `factor_source` column to wide form.
    LEFT JOIN tickers (for sector) and daily_prices (for market_cap +
    avg trading value).
    """
    if not factors:
        raise ValueError("load_factor_panel: `factors` must be non-empty")
    if factor_source not in _VALID_SOURCES:
        raise ValueError(
            f"load_factor_panel: factor_source must be one of "
            f"{_VALID_SOURCES}, got '{factor_source}'"
        )

    # Validate factor names are safe (only [A-Za-z0-9_]) before string formatting
    for f in factors:
        if not _is_safe_identifier(f):
            raise ValueError(
                f"load_factor_panel: factor name has unsafe chars: '{f}'"
            )

    # Build pivot SELECT — one MAX(CASE WHEN ...) per factor.
    pivot_cols: list[str] = []
    for f in factors:
        pivot_cols.append(
            f"MAX(CASE WHEN fs.factor_name = '{f}' THEN fs.{factor_source} END) "
            f"AS \"{f}\""
        )
    pivot_sql = ",\n            ".join(pivot_cols)

    # Subquery for avg trading value
    avg_value_select = ""
    avg_value_subq = ""
    if include_avg_value:
        avg_value_subq = """
            LEFT JOIN (
                SELECT symbol, AVG(value) AS avg_value_20d
                  FROM daily_prices
                 WHERE date BETWEEN :avg_start AND :asof
                   AND value IS NOT NULL
                 GROUP BY symbol
            ) av ON av.symbol = fs.symbol
        """
        avg_value_select = ", av.avg_value_20d"
    else:
        avg_value_select = ", NULL::BIGINT AS avg_value_20d"

    mcap_select = ""
    mcap_subq = ""
    if include_market_cap:
        mcap_subq = """
            LEFT JOIN daily_prices dp
                   ON dp.symbol = fs.symbol
                  AND dp.date = :asof
        """
        mcap_select = ", dp.market_cap"
    else:
        mcap_select = ", NULL::BIGINT AS market_cap"

    group_extra = ""
    if include_market_cap:
        group_extra += ", dp.market_cap"
    if include_avg_value:
        group_extra += ", av.avg_value_20d"

    sql = text(f"""
        SELECT
            fs.symbol,
            t.sector
            {mcap_select}
            {avg_value_select},
            {pivot_sql}
        FROM factor_signals fs
        LEFT JOIN tickers t ON t.symbol = fs.symbol
        {mcap_subq}
        {avg_value_subq}
        WHERE fs.date = :asof
          AND fs.factor_name = ANY(:factor_names)
          AND (fs.universe = :universe OR fs.universe IS NULL)
        GROUP BY fs.symbol, t.sector{group_extra}
    """)

    params: dict = {
        "asof": as_of,
        "factor_names": list(factors),
        "universe": universe,
    }
    if include_avg_value:
        window_start = as_of - timedelta(days=int(avg_value_lookback_days * 2) + 14)
        params["avg_start"] = window_start

    with get_engine().connect() as conn:
        df = pd.read_sql(sql, conn, params=params)

    if df.empty:
        logger.warning(
            f"load_factor_panel: no factor_signals on {as_of} for "
            f"universe={universe} factors={factors}"
        )
        return df

    # Numeric type cleanup
    for f in factors:
        if f in df.columns:
            df[f] = pd.to_numeric(df[f], errors="coerce")
    if "market_cap" in df.columns:
        df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
    if "avg_value_20d" in df.columns:
        df["avg_value_20d"] = pd.to_numeric(df["avg_value_20d"], errors="coerce")

    return df.reset_index(drop=True)


def factor_coverage(
    factors: list[str],
    universe: str,
    as_of: date,
) -> dict[str, int]:
    """For each factor, count how many symbols have a non-NULL rank_value
    on `as_of` in the given universe.

    Used by the bot runner to decide whether the day's signal is rich
    enough to act on.
    """
    if not factors:
        return {}
    sql = text("""
        SELECT factor_name, COUNT(*) AS n
          FROM factor_signals fs
         WHERE fs.date = :asof
           AND fs.factor_name = ANY(:factor_names)
           AND (fs.universe = :universe OR fs.universe IS NULL)
           AND fs.rank_value IS NOT NULL
         GROUP BY factor_name
    """)
    params: dict = {
        "asof": as_of,
        "factor_names": list(factors),
        "universe": universe,
    }
    with get_engine().connect() as conn:
        rows = conn.execute(sql, params).all()
    cov = {r[0]: int(r[1]) for r in rows}
    # Make sure missing factors appear as 0
    for f in factors:
        cov.setdefault(f, 0)
    return cov


def _is_safe_identifier(s: str) -> bool:
    """Allow only [A-Za-z0-9_] in factor names (defence-in-depth)."""
    if not s:
        return False
    return all(c.isalnum() or c == "_" for c in s)
