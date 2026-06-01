"""Survivorship-aware universe construction.

Why this module exists:
    Quant evaluation needs a "universe at time t" - the set of symbols
    that were actually tradable on date t. If we only use today's
    `tickers` table (which still excludes some delisted names), we get
    survivorship bias: the model learns "stocks that survived" rather
    than "stocks that will outperform". This module reconstructs a
    point-in-time universe from `tickers` (including delisted=TRUE
    rows) using listing_date / delisted_date when available.

Universe identifiers (the `name` argument):
    ALL        - All KOSPI + KOSDAQ tickers that were listed on as_of_date.
    KOSPI      - KOSPI only.
    KOSDAQ     - KOSDAQ only.
    KOSPI200   - KOSPI200 constituents (placeholder; needs index data).
                 For now falls back to "top 200 by recent market cap on
                 as_of_date" using daily_prices.market_cap, which is a
                 reasonable approximation but NOT the official index.

What this does NOT do:
    - Halted / suspended trading filtering: that needs intra-day or
      KIS data, which is out of scope for Phase 1. Halted days will
      show up as missing fwd_return rows downstream.
    - Tradability filtering by liquidity: a separate optional filter
      (see filter_by_liquidity) checks recent average trading value.
      Use it explicitly when needed.

Return type:
    Always `list[str]` of 6-digit KRX symbols, sorted ascending.
    Empty list if no symbols match (e.g. universe="KOSPI200" but
    daily_prices is empty for the date).
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text

from src.db.connection import get_engine, session_scope
from src.utils.logger import logger

_MARKET_TO_CORP_CLS: dict[str, str] = {
    "KOSPI": "Y",
    "KOSDAQ": "K",
    "KONEX": "N",
    "기타": "E",
}

# Canonical universe names. Anything else is rejected by get_universe.
SUPPORTED_UNIVERSES = ("ALL", "KOSPI", "KOSDAQ", "KOSPI200")


def get_universe(
    name: str,
    as_of_date: date,
    *,
    min_market_cap_krw: int | None = None,
) -> list[str]:
    """Return the list of symbols that constituted `name` on `as_of_date`.

    Args:
        name: One of SUPPORTED_UNIVERSES (case-insensitive).
        as_of_date: Point-in-time anchor. A symbol is included only if
                    delisted_date IS NULL OR delisted_date > as_of_date.
                    (listing_date 컬럼은 018 마이그레이션에서 제거됨)
        min_market_cap_krw: Optional market-cap floor in KRW. Applied
                           using daily_prices.market_cap from the closest
                           prior trading day (up to 7 days back). Useful
                           to drop micro-caps. None = no filter.

    Returns:
        Sorted list of 6-digit symbols. Empty if nothing matches.

    Notes:
        - The `tickers` table is the source of truth for listing /
          delisted status. If your collector hasn't populated
          listing_date for older symbols, those rows are still included
          (we don't drop NULL listing_date) - this means a small bias
          remains until ticker master is fully backfilled.
        - KOSPI200 currently approximated as "top 200 by market cap" -
          see module docstring.
    """
    upper = name.upper()
    if upper not in SUPPORTED_UNIVERSES:
        raise ValueError(
            f"Unknown universe '{name}'. Supported: {SUPPORTED_UNIVERSES}"
        )

    if upper == "KOSPI200":
        return _kospi200_approx(as_of_date)

    # ALL / KOSPI / KOSDAQ -> filter tickers table
    markets: list[str] | None
    if upper == "ALL":
        markets = ["KOSPI", "KOSDAQ"]
    else:
        markets = [upper]

    corp_cls_values = [_MARKET_TO_CORP_CLS.get(m, m) for m in markets]
    where = ["corp_cls = ANY(:corp_cls_values)"]
    params: dict = {"corp_cls_values": corp_cls_values, "asof": as_of_date}

    # listing_date 컬럼은 018 마이그레이션에서 제거됨.
    # 상장일 필터는 daily_prices.MIN(date)로 파생할 수 있으나
    # 현재는 survivorship 완화를 위해 생략하고 delisted_date만 사용.
    where.append(
        "(delisted = FALSE OR delisted_date IS NULL OR delisted_date > :asof)"
    )

    sql = text(
        "SELECT symbol FROM tickers "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY symbol"
    )
    with session_scope() as session:
        symbols = [r[0] for r in session.execute(sql, params).all()]

    if min_market_cap_krw is not None and symbols:
        symbols = _filter_by_market_cap(symbols, as_of_date, min_market_cap_krw)

    logger.debug(
        f"get_universe({name}, {as_of_date}) -> {len(symbols)} symbols"
    )
    return symbols


def get_universe_panel(
    name: str,
    start: date,
    end: date,
) -> pd.DataFrame:
    """Return a long-format DataFrame of (date, symbol) pairs that were
    in `name` on each business day in [start, end].

    This is the canonical input for cross-sectional evaluation: every
    (date, symbol) row says "on this date, this symbol was eligible
    for the universe". Forward-returns and factor-signals are then
    joined onto this panel.

    Implementation detail:
        We could call get_universe() for every business day, but for
        most universes the daily set changes very slowly. To keep the
        function correct without being silly-slow, we still iterate
        per business day - it's cheap because each call is one SQL
        query (the panel may have ~250 days x 1 query = 250 queries
        over a year, which is fine for offline evaluation but NOT for
        hot paths; callers that need real-time speed should cache).

    Returns:
        DataFrame with columns [date, symbol], sorted by (date, symbol).
        Empty DataFrame if nothing matches.
    """
    if start > end:
        raise ValueError(f"start {start} > end {end}")

    rows: list[tuple[date, str]] = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri; holiday holes are dropped later
            syms = get_universe(name, d)
            rows.extend((d, s) for s in syms)
        d += timedelta(days=1)

    if not rows:
        return pd.DataFrame(columns=["date", "symbol"])
    df = pd.DataFrame(rows, columns=["date", "symbol"])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def _kospi200_approx(as_of_date: date) -> list[str]:
    """Approximate KOSPI200 as the top-200 KOSPI symbols by market cap
    using the closest prior daily_prices row.

    This is intentionally a stop-gap: the real KOSPI200 constituent list
    is published by KRX and rebalanced quarterly. Replace this with the
    real list when an index-membership collector is added.
    """
    # Pull KOSPI symbols that were listed on as_of_date
    base = get_universe("KOSPI", as_of_date)
    if not base:
        return []

    # Use daily_prices.market_cap from the closest trading day at or before
    # as_of_date. Window 7 days to bridge over weekends/holidays.
    lookback = as_of_date - timedelta(days=7)
    sql = text("""
        SELECT DISTINCT ON (symbol) symbol, market_cap, date
          FROM daily_prices
         WHERE symbol = ANY(:syms)
           AND date BETWEEN :lb AND :asof
           AND market_cap IS NOT NULL
         ORDER BY symbol, date DESC
    """)
    with get_engine().connect() as conn:
        df = pd.read_sql(
            sql, conn,
            params={"syms": base, "lb": lookback, "asof": as_of_date},
        )

    if df.empty:
        logger.warning(
            f"KOSPI200 approx: no market_cap data near {as_of_date} - "
            "returning empty universe. Backfill pykrx to populate "
            "market_cap."
        )
        return []

    df = df.sort_values("market_cap", ascending=False).head(200)
    return sorted(df["symbol"].tolist())


def _filter_by_market_cap(
    symbols: list[str],
    as_of_date: date,
    min_market_cap_krw: int,
) -> list[str]:
    """Keep only symbols whose latest market_cap (within 7 days of
    as_of_date) is >= min_market_cap_krw. Symbols without market_cap
    data are dropped (conservative)."""
    lookback = as_of_date - timedelta(days=7)
    sql = text("""
        SELECT DISTINCT ON (symbol) symbol, market_cap
          FROM daily_prices
         WHERE symbol = ANY(:syms)
           AND date BETWEEN :lb AND :asof
           AND market_cap IS NOT NULL
         ORDER BY symbol, date DESC
    """)
    with get_engine().connect() as conn:
        df = pd.read_sql(
            sql, conn,
            params={"syms": symbols, "lb": lookback, "asof": as_of_date},
        )
    if df.empty:
        return []
    kept = df[df["market_cap"] >= min_market_cap_krw]["symbol"].tolist()
    return sorted(kept)


def filter_by_liquidity(
    symbols: list[str],
    as_of_date: date,
    lookback_days: int = 20,
    min_avg_value_krw: int = 100_000_000,
) -> list[str]:
    """Optional liquidity filter: drop symbols whose mean daily trading
    value over the last `lookback_days` is below `min_avg_value_krw`.

    Default threshold (1억원/day) is a reasonable cutoff to ensure the
    signal is implementable by a small/mid-size allocation.

    NULL value entries (from FDR which doesn't have `value`) are treated
    as missing - symbols with no `value` data are kept (don't drop them
    just because data source didn't report it).
    """
    if not symbols:
        return []
    start = as_of_date - timedelta(days=lookback_days * 2)  # buffer for holidays
    sql = text("""
        SELECT symbol, AVG(value) AS avg_value
          FROM daily_prices
         WHERE symbol = ANY(:syms)
           AND date BETWEEN :start AND :asof
           AND value IS NOT NULL
         GROUP BY symbol
    """)
    with get_engine().connect() as conn:
        df = pd.read_sql(
            sql, conn,
            params={"syms": symbols, "start": start, "asof": as_of_date},
        )

    if df.empty:
        # No value data anywhere - return original list unfiltered
        return sorted(symbols)

    kept = set(df[df["avg_value"] >= min_avg_value_krw]["symbol"])
    # Also keep symbols that had NO `value` rows at all (conservative)
    had_data = set(df["symbol"])
    no_data = set(symbols) - had_data
    return sorted(kept | no_data)


if __name__ == "__main__":
    # Smoke test
    from datetime import date as _date
    today = _date.today()
    for u in SUPPORTED_UNIVERSES:
        syms = get_universe(u, today)
        logger.info(f"  {u}: {len(syms)} symbols")
