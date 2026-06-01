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

from src.db.connection import get_engine, raw_connection, session_scope
from src.db.models import (
    CollectionLog,
    ConsensusEstimate,
    DartCorpCode,
    DartDisclosure,
    DartFinancial,
    DartIndicator,
    EvalMetric,
    EvalRun,
    FactorSignal,
    ForwardReturn,
    ModelFeatureImportance,
    ModelPrediction,
    ModelRun,
    Ticker,
    TickerUS,
)
from src.utils.logger import logger


# -------------------- Tickers --------------------

# tickers.corp_cls 도메인: Y=KOSPI, K=KOSDAQ, N=KONEX, E=기타.
# 설정 파일/호출자는 여전히 "KOSPI"/"KOSDAQ" 스타일 이름을 쓰므로 여기서 변환.
_MARKET_TO_CORP_CLS: dict[str, str] = {
    "KOSPI": "Y",
    "KOSDAQ": "K",
    "KONEX": "N",
    "기타": "E",
}


def get_active_tickers(markets: list[str] | None = None) -> list[Ticker]:
    with session_scope() as session:
        stmt = select(Ticker).where(Ticker.delisted.is_(False))
        if markets:
            corp_cls_values = [_MARKET_TO_CORP_CLS.get(m, m) for m in markets]
            stmt = stmt.where(Ticker.corp_cls.in_(corp_cls_values))
        stmt = stmt.order_by(Ticker.symbol)
        return list(session.execute(stmt).scalars().all())


# -------------------- Daily prices --------------------

DAILY_COLUMNS = [
    "symbol", "date", "open", "high", "low", "close",
    "volume", "value", "market_cap", "shares",
    "foreign_net", "institution_net", "individual_net",
    "per", "pbr", "dividend_yield",
]

DAILY_RAW_COLUMNS = [
    "symbol", "date", "open", "high", "low", "close",
    "volume", "value",
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


def upsert_daily_prices_raw(df: pd.DataFrame) -> int:
    """Bulk upsert daily_prices_raw via COPY + ON CONFLICT.

    Mirrors `upsert_daily_prices` but writes the unadjusted (raw) OHLCV
    table populated by daily_kis collector with FID_ORG_ADJ_PRC=1.

    Expects df columns to be a subset of DAILY_RAW_COLUMNS
    (symbol, date, open, high, low, close, volume, value).
    Missing columns will be NULLed out.

    No fundamentals (market_cap/per/pbr/...) here — those live only in
    daily_prices because they're price-adjustment-independent.
    """
    if df.empty:
        return 0

    for col in DAILY_RAW_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[DAILY_RAW_COLUMNS].copy()

    buf = StringIO()
    df.to_csv(buf, index=False, header=False, sep="\t", na_rep="\\N")
    buf.seek(0)

    col_list = ", ".join(DAILY_RAW_COLUMNS)
    update_cols = [c for c in DAILY_RAW_COLUMNS if c not in ("symbol", "date")]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    with raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE tmp_daily_raw "
                "(LIKE daily_prices_raw INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            )
            with cur.copy(
                f"COPY tmp_daily_raw ({col_list}) FROM STDIN "
                f"WITH (FORMAT CSV, DELIMITER E'\\t', NULL '\\N')"
            ) as copy:
                copy.write(buf.read())
            cur.execute(
                f"INSERT INTO daily_prices_raw ({col_list}) "
                f"SELECT {col_list} FROM tmp_daily_raw "
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
            "KOSPI"/"KOSDAQ" 형식 또는 corp_cls 값("Y"/"K") 모두 허용.
        limit: optional LIMIT. Results are ordered by (date DESC, symbol).

    Example:
        df = query_daily_with_names(
            symbols=["005930", "000660"],
            start=date(2025, 1, 1),
        )
        # df.columns: symbol, name, corp_cls, induty_code, date, open, ...
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
        corp_cls_values = [_MARKET_TO_CORP_CLS.get(m, m) for m in markets]
        where.append("corp_cls = ANY(:corp_cls_values)")
        params["corp_cls_values"] = corp_cls_values

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    limit_sql = f"LIMIT {int(limit)}" if limit else ""

    sql = text(
        f"SELECT * FROM v_daily_prices {where_sql} "
        f"ORDER BY date DESC, symbol {limit_sql}"
    )

    with get_engine().connect() as conn:
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


# -------------------- DART (corp_codes & disclosures) --------------------

DART_CORP_CODE_COLUMNS = [
    "corp_code", "corp_name", "stock_code", "modify_date",
]
DART_DISCLOSURE_COLUMNS = [
    "rcept_no", "corp_code", "corp_name", "stock_code", "corp_cls",
    "report_nm", "rcept_dt", "flr_nm", "rm", "kind", "kind_detail",
]


def upsert_dart_corp_codes(df: pd.DataFrame) -> int:
    """Bulk upsert dart_corp_codes via COPY + ON CONFLICT.

    Source: DART corpCode.xml ZIP. ~100k rows expected, replaced in full
    on every refresh — ON CONFLICT DO UPDATE keeps existing PKs while
    refreshing name/stock_code/modify_date.

    Expects df columns: corp_code, corp_name, stock_code, modify_date.
    Missing columns are NULLed out.
    """
    if df.empty:
        return 0

    for col in DART_CORP_CODE_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[DART_CORP_CODE_COLUMNS].copy()

    buf = StringIO()
    df.to_csv(buf, index=False, header=False, sep="\t", na_rep="\\N")
    buf.seek(0)

    col_list = ", ".join(DART_CORP_CODE_COLUMNS)
    update_cols = [c for c in DART_CORP_CODE_COLUMNS if c != "corp_code"]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
    update_clause += ", updated_at = NOW()"

    with raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE tmp_dart_corp "
                "(LIKE dart_corp_codes INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            )
            with cur.copy(
                f"COPY tmp_dart_corp ({col_list}) FROM STDIN "
                f"WITH (FORMAT CSV, DELIMITER E'\\t', NULL '\\N')"
            ) as copy:
                copy.write(buf.read())
            cur.execute(
                f"INSERT INTO dart_corp_codes ({col_list}) "
                f"SELECT {col_list} FROM tmp_dart_corp "
                f"ON CONFLICT (corp_code) DO UPDATE SET {update_clause}"
            )
            affected = cur.rowcount
    return affected


def upsert_dart_disclosures(df: pd.DataFrame) -> int:
    """Bulk upsert dart_disclosures via COPY + ON CONFLICT.

    PK is rcept_no (14-digit). DART can re-issue the same rcept_no with
    updated metadata when a disclosure is amended (rm field changes),
    so we DO update on conflict.
    """
    if df.empty:
        return 0

    for col in DART_DISCLOSURE_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[DART_DISCLOSURE_COLUMNS].copy()

    buf = StringIO()
    df.to_csv(buf, index=False, header=False, sep="\t", na_rep="\\N")
    buf.seek(0)

    col_list = ", ".join(DART_DISCLOSURE_COLUMNS)
    update_cols = [c for c in DART_DISCLOSURE_COLUMNS if c != "rcept_no"]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    with raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE tmp_dart_disc "
                "(LIKE dart_disclosures INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            )
            with cur.copy(
                f"COPY tmp_dart_disc ({col_list}) FROM STDIN "
                f"WITH (FORMAT CSV, DELIMITER E'\\t', NULL '\\N')"
            ) as copy:
                copy.write(buf.read())
            cur.execute(
                f"INSERT INTO dart_disclosures ({col_list}) "
                f"SELECT {col_list} FROM tmp_dart_disc "
                f"ON CONFLICT (rcept_no) DO UPDATE SET {update_clause}"
            )
            affected = cur.rowcount
    return affected


def get_corp_code_by_stock(stock_code: str) -> str | None:
    """Look up DART corp_code from a 6-digit stock_code.

    Returns None if the stock is unknown to DART (very rare for listed
    securities, but possible for newly-listed names before the next
    DART corp-code refresh).
    """
    with session_scope() as session:
        stmt = (
            select(DartCorpCode.corp_code)
            .where(DartCorpCode.stock_code == stock_code)
            .limit(1)
        )
        row = session.execute(stmt).first()
        return row[0] if row else None


def get_dart_corp_codes_freshness() -> datetime | None:
    """Return the most recent updated_at across dart_corp_codes.

    Used by the corp_codes collector to decide whether to refresh based
    on a stale-after-N-days policy. Returns None if the table is empty.
    """
    with session_scope() as session:
        stmt = text("SELECT MAX(updated_at) FROM dart_corp_codes")
        row = session.execute(stmt).first()
        return row[0] if row and row[0] else None


# -------------------- DART financials & indicators (Phase 2) --------------------

# Order matches the columns in dart_financials. account_nm is the last
# PK component because it's the most variable; ordering this way keeps
# the COPY column list aligned with how DART returns data.
DART_FINANCIAL_COLUMNS = [
    "corp_code", "bsns_year", "reprt_code", "fs_div", "account_nm",
    "sj_div", "account_id",
    "thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount",
    "thstrm_add_amount",
    "currency", "ord", "source",
]
DART_INDICATOR_COLUMNS = [
    "corp_code", "bsns_year", "reprt_code", "fs_div", "idx_nm",
    "idx_cl_code", "idx_cl_nm", "idx_code",
    "thstrm_value", "frmtrm_value", "bfefrmtrm_value",
    "source",
]

# PK columns to skip from UPDATE SET on conflict (they're already matched).
_DART_FIN_PK = ("corp_code", "bsns_year", "reprt_code", "fs_div", "account_nm")
_DART_IDX_PK = ("corp_code", "bsns_year", "reprt_code", "fs_div", "idx_nm")


def upsert_dart_financials(df: pd.DataFrame) -> int:
    """Bulk upsert dart_financials via COPY + ON CONFLICT.

    Re-run safety: PK is the natural composite key, so re-collecting the
    same (corp, year, reprt, fs_div) overwrites cleanly.
    """
    if df.empty:
        return 0

    for col in DART_FINANCIAL_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[DART_FINANCIAL_COLUMNS].copy()

    buf = StringIO()
    df.to_csv(buf, index=False, header=False, sep="\t", na_rep="\\N")
    buf.seek(0)

    col_list = ", ".join(DART_FINANCIAL_COLUMNS)
    update_cols = [c for c in DART_FINANCIAL_COLUMNS if c not in _DART_FIN_PK]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    with raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE tmp_dart_fin "
                "(LIKE dart_financials INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            )
            with cur.copy(
                f"COPY tmp_dart_fin ({col_list}) FROM STDIN "
                f"WITH (FORMAT CSV, DELIMITER E'\\t', NULL '\\N')"
            ) as copy:
                copy.write(buf.read())
            cur.execute(
                f"INSERT INTO dart_financials ({col_list}) "
                f"SELECT {col_list} FROM tmp_dart_fin "
                f"ON CONFLICT (corp_code, bsns_year, reprt_code, fs_div, account_nm) "
                f"DO UPDATE SET {update_clause}"
            )
            affected = cur.rowcount
    return affected


def upsert_dart_indicators(df: pd.DataFrame) -> int:
    """Bulk upsert dart_indicators via COPY + ON CONFLICT."""
    if df.empty:
        return 0

    for col in DART_INDICATOR_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[DART_INDICATOR_COLUMNS].copy()

    buf = StringIO()
    df.to_csv(buf, index=False, header=False, sep="\t", na_rep="\\N")
    buf.seek(0)

    col_list = ", ".join(DART_INDICATOR_COLUMNS)
    update_cols = [c for c in DART_INDICATOR_COLUMNS if c not in _DART_IDX_PK]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    with raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE tmp_dart_idx "
                "(LIKE dart_indicators INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            )
            with cur.copy(
                f"COPY tmp_dart_idx ({col_list}) FROM STDIN "
                f"WITH (FORMAT CSV, DELIMITER E'\\t', NULL '\\N')"
            ) as copy:
                copy.write(buf.read())
            cur.execute(
                f"INSERT INTO dart_indicators ({col_list}) "
                f"SELECT {col_list} FROM tmp_dart_idx "
                f"ON CONFLICT (corp_code, bsns_year, reprt_code, fs_div, idx_nm) "
                f"DO UPDATE SET {update_clause}"
            )
            affected = cur.rowcount
    return affected


def get_listed_corp_codes() -> list[tuple[str, str]]:
    """Return (corp_code, stock_code) for all listed Korean companies.

    Used by financial/indicator collectors to iterate over the universe.
    Drops anything where stock_code is NULL (non-listed) or where the
    company is flagged delisted in the local tickers table (best-effort).
    """
    with session_scope() as session:
        # Pull DART corp_codes that have a stock_code, then filter against
        # the local tickers table to exclude delisted symbols.
        stmt = text("""
            SELECT c.corp_code, c.stock_code
              FROM dart_corp_codes c
              LEFT JOIN tickers t ON t.symbol = c.stock_code
             WHERE c.stock_code IS NOT NULL
               AND (t.delisted IS NULL OR t.delisted = FALSE)
             ORDER BY c.stock_code
        """)
        return [(r[0], r[1]) for r in session.execute(stmt).all()]


def get_completed_dart_financials_keys(
    bsns_year: int,
    reprt_code: str,
    fs_div: str,
) -> set[str]:
    """Return corp_codes already collected for (year, reprt_code, fs_div).

    Used by the collector to skip combinations already done. We define
    'collected' as 'has at least one row in dart_financials for this PK
    triple', not 'has a success log entry' — that's because the data
    presence itself is the source of truth and survives log table resets.
    """
    with session_scope() as session:
        stmt = text("""
            SELECT DISTINCT corp_code
              FROM dart_financials
             WHERE bsns_year = :y AND reprt_code = :r AND fs_div = :f
        """)
        return {
            r[0] for r in session.execute(
                stmt, {"y": bsns_year, "r": reprt_code, "f": fs_div}
            ).all()
        }


def get_completed_dart_indicators_keys(
    bsns_year: int,
    reprt_code: str,
    fs_div: str,
) -> set[str]:
    """Same as get_completed_dart_financials_keys but for indicators."""
    with session_scope() as session:
        stmt = text("""
            SELECT DISTINCT corp_code
              FROM dart_indicators
             WHERE bsns_year = :y AND reprt_code = :r AND fs_div = :f
        """)
        return {
            r[0] for r in session.execute(
                stmt, {"y": bsns_year, "r": reprt_code, "f": fs_div}
            ).all()
        }


# -------------------- Consensus estimates (FnGuide) --------------------

# Column order MUST match migrations/012_consensus_fnguide.sql CREATE TABLE.
CONSENSUS_COLUMNS = [
    "symbol",
    "as_of_date",
    "fiscal_period",
    "fiscal_period_type",
    "eps_estimate",
    "revenue_estimate",
    "op_income_estimate",
    "net_income_estimate",
    "target_price",
    "opinion",
    "n_estimates",
    "source",
]

_CONSENSUS_PK = ("symbol", "as_of_date", "fiscal_period")


def upsert_consensus_estimates(df: pd.DataFrame) -> int:
    """Bulk upsert consensus_estimates via COPY + ON CONFLICT.

    Expects df columns to be a subset of CONSENSUS_COLUMNS. Missing
    columns are NULLed out (except non-nullable fiscal_period_type and
    source, which the caller must always supply).

    Re-run safety: PK is (symbol, as_of_date, fiscal_period), so the
    same day's collection can be re-run safely.
    """
    if df.empty:
        return 0

    for col in CONSENSUS_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[CONSENSUS_COLUMNS].copy()

    buf = StringIO()
    df.to_csv(buf, index=False, header=False, sep="\t", na_rep="\\N")
    buf.seek(0)

    col_list = ", ".join(CONSENSUS_COLUMNS)
    update_cols = [c for c in CONSENSUS_COLUMNS if c not in _CONSENSUS_PK]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    with raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE tmp_consensus "
                "(LIKE consensus_estimates INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            )
            with cur.copy(
                f"COPY tmp_consensus ({col_list}) FROM STDIN "
                f"WITH (FORMAT CSV, DELIMITER E'\\t', NULL '\\N')"
            ) as copy:
                copy.write(buf.read())
            cur.execute(
                f"INSERT INTO consensus_estimates ({col_list}) "
                f"SELECT {col_list} FROM tmp_consensus "
                f"ON CONFLICT (symbol, as_of_date, fiscal_period) "
                f"DO UPDATE SET {update_clause}"
            )
            affected = cur.rowcount
    return affected


HANKYUNG_COLUMNS = [
    "report_idx",
    "business_code",
    "business_name",
    "report_date",
    "grade_value",
    "target_price",
    "prev_target_price",
    "change_price",
    "analyst",
    "office_name",
    "report_title",
]

_HANKYUNG_PK = ("report_idx",)


def upsert_hankyung_reports(df: pd.DataFrame) -> int:
    """Bulk upsert hankyung_reports via COPY + ON CONFLICT.

    PK: report_idx. 동일 report_idx 재수집 시 모든 컬럼 UPDATE.
    """
    if df.empty:
        return 0

    for col in HANKYUNG_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[HANKYUNG_COLUMNS].copy()

    buf = StringIO()
    df.to_csv(buf, index=False, header=False, sep="\t", na_rep="\\N")
    buf.seek(0)

    col_list = ", ".join(HANKYUNG_COLUMNS)
    update_cols = [c for c in HANKYUNG_COLUMNS if c not in _HANKYUNG_PK]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    with raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE tmp_hankyung "
                "(LIKE hankyung_reports INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            )
            with cur.copy(
                f"COPY tmp_hankyung ({col_list}) FROM STDIN "
                f"WITH (FORMAT CSV, DELIMITER E'\\t', NULL '\\N')"
            ) as copy:
                copy.write(buf.read())
            cur.execute(
                f"INSERT INTO hankyung_reports ({col_list}) "
                f"SELECT {col_list} FROM tmp_hankyung "
                f"ON CONFLICT (report_idx) "
                f"DO UPDATE SET {update_clause}"
            )
            affected = cur.rowcount
    return affected


def get_completed_symbols_on_date(
    collector: str,
    as_of_date: date,
) -> set[str]:
    """Return symbols already collected on `as_of_date` for `collector`.

    Used by daily-snapshot collectors (e.g. consensus_fnguide) to resume
    an interrupted run. A symbol counts as 'done' iff there's a success
    log entry with target_date = as_of_date AND rows_inserted > 0.

    Different from `get_completed_symbols_in_range`:
        - That one is for symbol-iterating range-backfills where the
          range's `end` date is used as the log marker.
        - This one is for daily snapshots where each day is one
          target_date and each symbol gets exactly one log row per day.
        - Functionally similar; kept separate so the caller's intent is
          self-documenting at call sites.
    """
    with session_scope() as session:
        stmt = text("""
            SELECT DISTINCT symbol
              FROM collection_log
             WHERE collector = :col
               AND status    = 'success'
               AND target_date = :as_of
               AND symbol IS NOT NULL
               AND rows_inserted > 0
        """)
        return {
            r[0] for r in session.execute(
                stmt, {"col": collector, "as_of": as_of_date}
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


# -------------------- Phase 1 research infra --------------------
# Forward returns & factor signals (cross-sectional alpha evaluation).
# Tables created by migrations/014_research_infra.sql.

FORWARD_RETURN_COLUMNS = [
    "symbol", "date", "horizon_days",
    "fwd_return", "high_return",
    "universe", "source",
]
_FWD_PK = ("symbol", "date", "horizon_days")

FACTOR_SIGNAL_COLUMNS = [
    "symbol", "date", "factor_name",
    "raw_value", "neutral_value", "rank_value", "z_score",
    "universe",
]
_FACTOR_PK = ("symbol", "date", "factor_name")


def upsert_forward_returns(df: pd.DataFrame) -> int:
    """Bulk upsert forward_returns via COPY + ON CONFLICT.

    Expects df columns to be a subset of FORWARD_RETURN_COLUMNS. Missing
    columns are NULLed out (except PK columns which are required).

    Re-run safety: PK is the natural composite key, so re-computing the
    same (symbol, date, horizon_days) overwrites cleanly. This matters
    because forward returns get recomputed every time daily_prices is
    backfilled with newer dates.
    """
    if df.empty:
        return 0

    for col in FORWARD_RETURN_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[FORWARD_RETURN_COLUMNS].copy()

    buf = StringIO()
    df.to_csv(buf, index=False, header=False, sep="\t", na_rep="\\N")
    buf.seek(0)

    col_list = ", ".join(FORWARD_RETURN_COLUMNS)
    update_cols = [c for c in FORWARD_RETURN_COLUMNS if c not in _FWD_PK]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    with raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE tmp_fwd_returns "
                "(LIKE forward_returns INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            )
            with cur.copy(
                f"COPY tmp_fwd_returns ({col_list}) FROM STDIN "
                f"WITH (FORMAT CSV, DELIMITER E'\\t', NULL '\\N')"
            ) as copy:
                copy.write(buf.read())
            cur.execute(
                f"INSERT INTO forward_returns ({col_list}) "
                f"SELECT {col_list} FROM tmp_fwd_returns "
                f"ON CONFLICT (symbol, date, horizon_days) "
                f"DO UPDATE SET {update_clause}"
            )
            affected = cur.rowcount
    return affected


def upsert_factor_signals(df: pd.DataFrame) -> int:
    """Bulk upsert factor_signals via COPY + ON CONFLICT.

    Expects df columns to be a subset of FACTOR_SIGNAL_COLUMNS. Missing
    columns are NULLed out.
    """
    if df.empty:
        return 0

    for col in FACTOR_SIGNAL_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[FACTOR_SIGNAL_COLUMNS].copy()

    buf = StringIO()
    df.to_csv(buf, index=False, header=False, sep="\t", na_rep="\\N")
    buf.seek(0)

    col_list = ", ".join(FACTOR_SIGNAL_COLUMNS)
    update_cols = [c for c in FACTOR_SIGNAL_COLUMNS if c not in _FACTOR_PK]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    with raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE tmp_factor_signals "
                "(LIKE factor_signals INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            )
            with cur.copy(
                f"COPY tmp_factor_signals ({col_list}) FROM STDIN "
                f"WITH (FORMAT CSV, DELIMITER E'\\t', NULL '\\N')"
            ) as copy:
                copy.write(buf.read())
            cur.execute(
                f"INSERT INTO factor_signals ({col_list}) "
                f"SELECT {col_list} FROM tmp_factor_signals "
                f"ON CONFLICT (symbol, date, factor_name) "
                f"DO UPDATE SET {update_clause}"
            )
            affected = cur.rowcount
    return affected


def query_forward_returns(
    horizon_days: int,
    start: date | None = None,
    end: date | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    """Read forward_returns into a DataFrame.

    Returns columns: symbol, date, horizon_days, fwd_return, high_return.
    Order: (date, symbol). Suitable for cross-sectional IC calculation
    via groupby('date').
    """
    where = ["horizon_days = :h"]
    params: dict = {"h": horizon_days}
    if start is not None:
        where.append("date >= :s")
        params["s"] = start
    if end is not None:
        where.append("date <= :e")
        params["e"] = end
    if symbols:
        where.append("symbol = ANY(:syms)")
        params["syms"] = list(symbols)
    sql = text(
        "SELECT symbol, date, horizon_days, fwd_return, high_return "
        "FROM forward_returns "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY date, symbol"
    )
    with get_engine().connect() as conn:
        return pd.read_sql(sql, conn, params=params)


def query_factor_signals(
    factor_name: str,
    start: date | None = None,
    end: date | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    """Read factor_signals into a DataFrame.

    Returns columns: symbol, date, factor_name, raw_value, neutral_value,
    rank_value, z_score. Order: (date, symbol).
    """
    where = ["factor_name = :f"]
    params: dict = {"f": factor_name}
    if start is not None:
        where.append("date >= :s")
        params["s"] = start
    if end is not None:
        where.append("date <= :e")
        params["e"] = end
    if symbols:
        where.append("symbol = ANY(:syms)")
        params["syms"] = list(symbols)
    sql = text(
        "SELECT symbol, date, factor_name, raw_value, neutral_value, "
        "rank_value, z_score "
        "FROM factor_signals "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY date, symbol"
    )
    with get_engine().connect() as conn:
        return pd.read_sql(sql, conn, params=params)


def insert_eval_run(
    run_id: str,
    factor_name: str,
    universe: str,
    start_date: date,
    end_date: date,
    horizon_days: int,
    params: dict | None = None,
    n_observations: int | None = None,
    notes: str | None = None,
) -> None:
    """Insert one row into eval_runs. Caller generates run_id.

    Recommended run_id format: 'YYYYMMDD_HHMMSS_<factor>_<universe>'
    (factor_eval.make_run_id helper produces this).
    """
    with session_scope() as session:
        session.add(EvalRun(
            run_id=run_id,
            factor_name=factor_name,
            universe=universe,
            start_date=start_date,
            end_date=end_date,
            horizon_days=horizon_days,
            params=params,
            n_observations=n_observations,
            notes=notes,
        ))


def insert_eval_metrics(
    run_id: str,
    metrics: dict[str, float | None],
) -> int:
    """Insert metrics for an existing run.

    `metrics` keys become metric_name, values become metric_value.
    NaN / None values are stored as NULL.
    Returns the number of rows inserted.
    """
    if not metrics:
        return 0

    import math
    rows = []
    for k, v in metrics.items():
        if v is None:
            mv = None
        elif isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            mv = None
        else:
            mv = v
        rows.append(EvalMetric(
            run_id=run_id, metric_name=k, metric_value=mv,
        ))

    with session_scope() as session:
        session.add_all(rows)
    return len(rows)


def get_eval_summary(
    factor_name: str | None = None,
    universe: str | None = None,
    limit: int = 50,
) -> pd.DataFrame:
    """Read v_eval_summary (the wide pivot of run + standard metrics).

    Useful for quickly comparing recent evaluations of one factor across
    universes, or comparing different factors on the same universe.
    """
    where = []
    params: dict = {}
    if factor_name is not None:
        where.append("factor_name = :f")
        params["f"] = factor_name
    if universe is not None:
        where.append("universe = :u")
        params["u"] = universe
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = text(
        f"SELECT * FROM v_eval_summary {where_sql} "
        f"ORDER BY created_at DESC LIMIT {int(limit)}"
    )
    with get_engine().connect() as conn:
        return pd.read_sql(sql, conn, params=params)


# -------------------- Phase 2 model infra --------------------
# ML model runs, predictions, feature importance.
# Tables created by migrations/015_model_runs.sql.

MODEL_PREDICTION_COLUMNS = [
    "model_run_id", "symbol", "date", "horizon_days",
    "y_pred", "y_true", "rank_value", "universe",
]
_MODEL_PRED_PK = ("model_run_id", "symbol", "date")


def insert_model_run(
    model_run_id: str,
    model_type: str,
    universe: str,
    train_start: date,
    train_end: date,
    test_start: date,
    test_end: date,
    horizon_days: int,
    target_type: str = "regression",
    features: list[str] | None = None,
    hyperparams: dict | None = None,
    n_train_rows: int | None = None,
    n_test_rows: int | None = None,
    train_loss: float | None = None,
    valid_loss: float | None = None,
    notes: str | None = None,
) -> None:
    """Insert one row into model_runs. Caller generates model_run_id.

    Recommended format: 'YYYYMMDD_HHMMSS_<type>_<universe>_fold<N>'.
    """
    with session_scope() as session:
        session.add(ModelRun(
            model_run_id=model_run_id,
            model_type=model_type,
            universe=universe,
            train_start=train_start, train_end=train_end,
            test_start=test_start, test_end=test_end,
            horizon_days=horizon_days,
            target_type=target_type,
            features=features,
            hyperparams=hyperparams,
            n_train_rows=n_train_rows,
            n_test_rows=n_test_rows,
            train_loss=train_loss,
            valid_loss=valid_loss,
            notes=notes,
        ))


def upsert_model_predictions(df: pd.DataFrame) -> int:
    """Bulk upsert model_predictions via COPY + ON CONFLICT.

    Expects df columns to be a subset of MODEL_PREDICTION_COLUMNS.
    Missing columns are NULLed out (except PK columns which are required).
    """
    if df.empty:
        return 0

    for col in MODEL_PREDICTION_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[MODEL_PREDICTION_COLUMNS].copy()

    buf = StringIO()
    df.to_csv(buf, index=False, header=False, sep="\t", na_rep="\\N")
    buf.seek(0)

    col_list = ", ".join(MODEL_PREDICTION_COLUMNS)
    update_cols = [c for c in MODEL_PREDICTION_COLUMNS if c not in _MODEL_PRED_PK]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    with raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE tmp_model_preds "
                "(LIKE model_predictions INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            )
            with cur.copy(
                f"COPY tmp_model_preds ({col_list}) FROM STDIN "
                f"WITH (FORMAT CSV, DELIMITER E'\\t', NULL '\\N')"
            ) as copy:
                copy.write(buf.read())
            cur.execute(
                f"INSERT INTO model_predictions ({col_list}) "
                f"SELECT {col_list} FROM tmp_model_preds "
                f"ON CONFLICT (model_run_id, symbol, date) "
                f"DO UPDATE SET {update_clause}"
            )
            affected = cur.rowcount
    return affected


def insert_feature_importance(
    model_run_id: str,
    importance: dict[str, float],
    importance_type: str = "gain",
) -> int:
    """Insert feature importance rows for a model run.

    `importance` keys = feature names, values = numeric importance.
    Auto-ranks descending and writes the rank column.
    Returns number of rows inserted.
    """
    if not importance:
        return 0
    # Sort descending by value for rank assignment
    items = sorted(importance.items(), key=lambda kv: -(kv[1] or 0.0))
    rows = []
    for rank_idx, (name, val) in enumerate(items, start=1):
        rows.append(ModelFeatureImportance(
            model_run_id=model_run_id,
            feature_name=name,
            importance_type=importance_type,
            importance=val,
            rank=rank_idx,
        ))
    with session_scope() as session:
        session.add_all(rows)
    return len(rows)


# -------------------- 분봉 가격 예측 --------------------

MINUTE_PRED_COLUMNS = [
    "symbol", "ts",
    "predicted_return", "predicted_close", "prev_close",
    "model_version", "feature_snapshot",
]
_MINUTE_PRED_PK = ("symbol", "ts")


def upsert_minute_price_predictions(df: pd.DataFrame) -> int:
    """Bulk upsert minute_price_predictions via COPY + ON CONFLICT.

    feature_snapshot (JSONB) 컬럼은 psycopg COPY 경로가 아닌 JSON 문자열로
    변환한 뒤 COPY 에 포함한다. NULL 이면 '\\N' 처리.

    Args:
        df: 컬럼이 MINUTE_PRED_COLUMNS 의 subset 인 DataFrame.
            ts 는 KST timezone-aware datetime 이어야 함.
    Returns:
        upsert 된 행 수.
    """
    if df.empty:
        return 0

    import json as _json

    df = df.copy()
    for col in MINUTE_PRED_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[MINUTE_PRED_COLUMNS].copy()

    # feature_snapshot JSONB → 문자열 직렬화 (COPY NULL='\N' 처리)
    if "feature_snapshot" in df.columns:
        df["feature_snapshot"] = df["feature_snapshot"].apply(
            lambda v: _json.dumps(v, ensure_ascii=False) if v is not None else None
        )

    buf = StringIO()
    df.to_csv(buf, index=False, header=False, sep="\t", na_rep="\\N")
    buf.seek(0)

    col_list = ", ".join(MINUTE_PRED_COLUMNS)
    update_cols = [c for c in MINUTE_PRED_COLUMNS if c not in _MINUTE_PRED_PK]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
    update_clause += ", updated_at = NOW()"

    with raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE tmp_mpp "
                "(LIKE minute_price_predictions INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            )
            with cur.copy(
                f"COPY tmp_mpp ({col_list}) FROM STDIN "
                f"WITH (FORMAT CSV, DELIMITER E'\\t', NULL '\\N')"
            ) as copy:
                copy.write(buf.read())
            cur.execute(
                f"INSERT INTO minute_price_predictions ({col_list}) "
                f"SELECT {col_list} FROM tmp_mpp "
                f"ON CONFLICT (symbol, ts) DO UPDATE SET {update_clause}"
            )
            affected = cur.rowcount
    return affected


def query_minute_price_predictions(
    symbol: str,
    ts_start: "datetime",
    ts_end: "datetime",
) -> pd.DataFrame:
    """특정 종목의 (ts_start, ts_end] 예측 분봉 조회.

    Returns:
        DataFrame [symbol, ts, predicted_return, predicted_close,
        prev_close, model_version], ts 오름차순 정렬.
    """
    sql = text(
        "SELECT symbol, ts, predicted_return, predicted_close, "
        "prev_close, model_version "
        "FROM minute_price_predictions "
        "WHERE symbol = :sym AND ts >= :s AND ts <= :e "
        "ORDER BY ts"
    )
    with get_engine().connect() as conn:
        return pd.read_sql(
            sql, conn,
            params={"sym": symbol, "s": ts_start, "e": ts_end},
        )


def query_model_predictions(
    model_run_id: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Read model_predictions.

    Returns columns: model_run_id, symbol, date, horizon_days,
    y_pred, y_true, rank_value, universe.
    """
    where = []
    params: dict = {}
    if model_run_id is not None:
        where.append("model_run_id = :rid")
        params["rid"] = model_run_id
    if start is not None:
        where.append("date >= :s")
        params["s"] = start
    if end is not None:
        where.append("date <= :e")
        params["e"] = end
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = text(
        "SELECT model_run_id, symbol, date, horizon_days, "
        "y_pred, y_true, rank_value, universe "
        f"FROM model_predictions {where_sql} "
        "ORDER BY date, symbol"
    )
    with get_engine().connect() as conn:
        return pd.read_sql(sql, conn, params=params)
