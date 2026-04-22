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
from src.db.models import CollectionLog, Ticker
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
