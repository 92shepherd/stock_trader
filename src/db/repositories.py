"""Repository layer.

Strategy:
    - Small reads/writes: SQLAlchemy ORM
    - Bulk inserts (daily/minute prices): psycopg3 COPY into a temp table,
      then INSERT ... ON CONFLICT from temp to main table.
      This is ~10x faster than row-by-row INSERT and handles duplicates.
"""
from __future__ import annotations

from datetime import date, datetime
from io import StringIO

import pandas as pd
from sqlalchemy import select, text

from src.db.connection import raw_connection, session_scope
from src.db.models import CollectionLog, Ticker, TickerUS
from src.utils.logger import logger


# -------------------- Tickers --------------------

def get_active_tickers(markets: list[str] | None = None) -> list[Ticker]:
    with session_scope() as session:
        stmt = select(Ticker).where(Ticker.delisted.is_(False))
        if markets:
            stmt = stmt.where(Ticker.market.in_(markets))
        stmt = stmt.order_by(Ticker.symbol)
        return list(session.execute(stmt).scalars().all())


# -------------------- Daily prices --------------------

DAILY_COLUMNS = [
    "symbol", "date", "open", "high", "low", "close",
    "volume", "value", "market_cap", "shares",
    "foreign_net", "institution_net", "individual_net",
    "per", "pbr", "dividend_yield",
]


def upsert_daily_prices(df: pd.DataFrame) -> int:
    """Bulk upsert daily_prices via COPY + ON CONFLICT.

    Expects df columns to be a subset of DAILY_COLUMNS.
    Missing columns will be NULLed out.
    Returns number of rows inserted/updated.
    """
    if df.empty:
        return 0

    # Align columns
    for col in DAILY_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[DAILY_COLUMNS].copy()

    buf = StringIO()
    df.to_csv(buf, index=False, header=False, sep="\t", na_rep="\\N")
    buf.seek(0)

    col_list = ", ".join(DAILY_COLUMNS)
    update_cols = [c for c in DAILY_COLUMNS if c not in ("symbol", "date")]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    with raw_connection() as conn:
        with conn.cursor() as cur:
            # 1) temp table mirroring daily_prices structure
            cur.execute(
                "CREATE TEMP TABLE tmp_daily "
                "(LIKE daily_prices INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            )
            # 2) COPY into temp
            with cur.copy(
                f"COPY tmp_daily ({col_list}) FROM STDIN "
                f"WITH (FORMAT CSV, DELIMITER E'\\t', NULL '\\N')"
            ) as copy:
                copy.write(buf.read())
            # 3) Merge into main table
            cur.execute(
                f"INSERT INTO daily_prices ({col_list}) "
                f"SELECT {col_list} FROM tmp_daily "
                f"ON CONFLICT (symbol, date) DO UPDATE SET {update_clause}"
            )
            affected = cur.rowcount
    return affected


def query_daily_with_names(
    symbols: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    markets: list[str] | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """Query v_daily_prices (daily_prices joined with tickers).

    Returns a pandas DataFrame with ticker name/sector/market alongside
    the OHLCV row — convenient for ad-hoc analysis where you want to see
    "삼성전자" instead of just "005930".

    Args:
        symbols: filter to these symbols. None = all.
        start: inclusive lower bound on date. None = no lower bound.
        end: inclusive upper bound on date. None = no upper bound.
        markets: filter to these markets (e.g. ["KOSPI"]). None = all.
        limit: optional LIMIT. Results are ordered by (date DESC, symbol).

    Example:
        df = query_daily_with_names(
            symbols=["005930", "000660"],
            start=date(2025, 1, 1),
        )
        # df.columns: symbol, name, market, sector, industry, date, open, ...
    """
    where = []
    params: dict = {}
    if symbols:
        where.append("symbol = ANY(:symbols)")
        params["symbols"] = list(symbols)
    if start is not None:
        where.append("date >= :start")
        params["start"] = start
    if end is not None:
        where.append("date <= :end")
        params["end"] = end
    if markets:
        where.append("market = ANY(:markets)")
        params["markets"] = list(markets)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    limit_sql = f"LIMIT {int(limit)}" if limit else ""

    sql = text(
        f"SELECT * FROM v_daily_prices {where_sql} "
        f"ORDER BY date DESC, symbol {limit_sql}"
    )

    with raw_connection() as conn:
        return pd.read_sql(sql, conn, params=params)


# -------------------- US tickers & daily prices --------------------

US_DAILY_COLUMNS = [
    "symbol", "date", "open", "high", "low", "close",
    "adj_close", "volume", "dividend", "split_ratio", "source",
]


def get_active_us_tickers(
    exchanges: list[str] | None = None,
    security_types: list[str] | None = None,
    include_test_issues: bool = False,
) -> list[TickerUS]:
    """Return non-delisted US tickers, optionally filtered.

    Args:
        exchanges: e.g. ["NASDAQ", "NYSE"]. None = all exchanges.
        security_types: e.g. ["COMMON", "ETF"]. None = all types.
        include_test_issues: if False (default), exclude tickers flagged
            as test_issue=True (NASDAQ Trader maintenance/test entries).
    """
    with session_scope() as session:
        stmt = select(TickerUS).where(TickerUS.delisted.is_(False))
        if exchanges:
            stmt = stmt.where(TickerUS.exchange.in_(exchanges))
        if security_types:
            stmt = stmt.where(TickerUS.security_type.in_(security_types))
        if not include_test_issues:
            stmt = stmt.where(TickerUS.test_issue.is_(False))
        stmt = stmt.order_by(TickerUS.symbol)
        return list(session.execute(stmt).scalars().all())


def upsert_daily_prices_us(df: pd.DataFrame) -> int:
    """Bulk upsert daily_prices_us via COPY + ON CONFLICT.

    Mirrors `upsert_daily_prices` (Korea) but writes to the US table.
    Expects df columns to be a subset of US_DAILY_COLUMNS; missing
    columns will be NULLed out.
    """
    if df.empty:
        return 0

    for col in US_DAILY_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[US_DAILY_COLUMNS].copy()

    buf = StringIO()
    df.to_csv(buf, index=False, header=False, sep="\t", na_rep="\\N")
    buf.seek(0)

    col_list = ", ".join(US_DAILY_COLUMNS)
    update_cols = [c for c in US_DAILY_COLUMNS if c not in ("symbol", "date")]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    with raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE tmp_daily_us "
                "(LIKE daily_prices_us INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            )
            with cur.copy(
                f"COPY tmp_daily_us ({col_list}) FROM STDIN "
                f"WITH (FORMAT CSV, DELIMITER E'\\t', NULL '\\N')"
            ) as copy:
                copy.write(buf.read())
            cur.execute(
                f"INSERT INTO daily_prices_us ({col_list}) "
                f"SELECT {col_list} FROM tmp_daily_us "
                f"ON CONFLICT (symbol, date) DO UPDATE SET {update_clause}"
            )
            affected = cur.rowcount
    return affected


def get_completed_us_symbols_in_range(
    collector: str,
    start: date,
    end: date,
) -> set[str]:
    """Resume helper for symbol-iterating US collectors.

    Same semantics as `get_completed_symbols_in_range` (Korea) — the
    function is duplicated rather than shared so each market can have
    independent resume logic if needed in the future. For now they are
    identical because `collection_log` is a single global table keyed
    by (collector, target_date, symbol).
    """
    _ = start  # preserved for API symmetry
    with session_scope() as session:
        stmt = text("""
            SELECT DISTINCT symbol
              FROM collection_log
             WHERE collector = :col
               AND status    = 'success'
               AND target_date = :end_date
               AND symbol IS NOT NULL
               AND rows_inserted > 0
        """)
        return {
            r[0] for r in session.execute(
                stmt, {"col": collector, "end_date": end}
            ).all()
        }


# -------------------- Minute prices --------------------

MINUTE_COLUMNS = ["symbol", "ts", "open", "high", "low", "close", "volume", "value"]


def upsert_minute_prices(df: pd.DataFrame) -> int:
    if df.empty:
        return 0

    for col in MINUTE_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[MINUTE_COLUMNS].copy()

    buf = StringIO()
    df.to_csv(buf, index=False, header=False, sep="\t", na_rep="\\N")
    buf.seek(0)

    col_list = ", ".join(MINUTE_COLUMNS)
    update_cols = [c for c in MINUTE_COLUMNS if c not in ("symbol", "ts")]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    with raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE tmp_minute "
                "(LIKE minute_prices INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            )
            with cur.copy(
                f"COPY tmp_minute ({col_list}) FROM STDIN "
                f"WITH (FORMAT CSV, DELIMITER E'\\t', NULL '\\N')"
            ) as copy:
                copy.write(buf.read())
            cur.execute(
                f"INSERT INTO minute_prices ({col_list}) "
                f"SELECT {col_list} FROM tmp_minute "
                f"ON CONFLICT (symbol, ts) DO UPDATE SET {update_clause}"
            )
            affected = cur.rowcount
    return affected


# -------------------- Collection log --------------------

def log_collection(
    collector: str,
    target_date: date,
    status: str,
    symbol: str | None = None,
    rows_inserted: int = 0,
    error_message: str | None = None,
    duration_ms: int | None = None,
) -> None:
    with session_scope() as session:
        session.add(CollectionLog(
            collector=collector,
            symbol=symbol,
            target_date=target_date,
            status=status,
            rows_inserted=rows_inserted,
            error_message=error_message,
            duration_ms=duration_ms,
        ))


def get_last_successful_date(collector: str, symbol: str | None = None) -> date | None:
    """Find the last date this collector successfully processed."""
    with session_scope() as session:
        stmt = text("""
            SELECT max(target_date) FROM collection_log
            WHERE collector = :col AND status = 'success'
              AND (:sym IS NULL OR symbol = :sym)
        """)
        row = session.execute(stmt, {"col": collector, "sym": symbol}).first()
        return row[0] if row and row[0] else None


def get_completed_symbols_in_range(
    collector: str,
    start: date,
    end: date,
) -> set[str]:
    """Return the set of symbols that have a 'success' log entry whose
    target_date == `end` AND rows_inserted > 0 for the given collector.

    Used by symbol-iterating collectors (e.g. daily_fdr) to skip symbols
    that already finished a [start, end] backfill in a prior run, so the
    backfill is resumable.

    Why target_date == end?
        Symbol-iterating backfills don't process a list of dates — they
        process one (symbol, range) at a time and write a single log row
        per symbol. We use the range's end date as the canonical marker
        for "this (symbol, range) is done". Caller is responsible for
        passing the same `end` it used when writing the success log.

    Args:
        collector: e.g. "daily_fdr".
        start: start of the backfill range (kept for symmetry; not used
               in the query — see note above).
        end: the range's end date, which is what `backfill_symbols`
             writes to `target_date` on success.

    Returns:
        Set of symbol strings already completed.
    """
    _ = start  # not used directly; preserved for API symmetry/future use
    with session_scope() as session:
        stmt = text("""
            SELECT DISTINCT symbol
              FROM collection_log
             WHERE collector = :col
               AND status    = 'success'
               AND target_date = :end_date
               AND symbol IS NOT NULL
               AND rows_inserted > 0
        """)
        return {
            r[0] for r in session.execute(
                stmt, {"col": collector, "end_date": end}
            ).all()
        }


def get_missing_dates(
    collector: str,
    start: date,
    end: date,
) -> list[date]:
    """Return business days in [start, end] that have no successful log entry."""
    with session_scope() as session:
        stmt = text("""
            SELECT target_date FROM collection_log
            WHERE collector = :col AND status = 'success'
              AND target_date BETWEEN :s AND :e
        """)
        done = {
            r[0] for r in session.execute(
                stmt, {"col": collector, "s": start, "e": end}
            ).all()
        }
    # Generate business days (Mon-Fri); holiday filtering is done by data source itself
    result = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in done:
            result.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return result
