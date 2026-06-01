"""Read-only query helpers for the MCP server.

These functions are NOT meant for the collection pipeline — they are
shaped specifically for ad-hoc questions an LLM might ask:
    - "삼성전자 최근 한 달 가격 보여줘"
    - "오늘 KOSPI 등락률 상위 10개"
    - "외국인 순매수 상위 종목"

Why not put these in src.db.repositories?
    The repositories module is the write/collection contract used by
    pipelines and skill-scaffolded collectors. Mixing analytics queries
    in there would blur its responsibility (and bloat its surface).
    These helpers live here, alongside the MCP tools that consume them,
    and are read-only by construction.

All queries hit `v_daily_prices` (a view that joins daily_prices with
tickers) so name/sector/industry are returned alongside numbers without
extra round-trips. The view is defined in
migrations/005_daily_prices_view.sql.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import text

from src.db.connection import session_scope

# corp_cls 변환 (tickers.corp_cls 도메인: Y=KOSPI, K=KOSDAQ, N=KONEX, E=기타)
_MARKET_TO_CORP_CLS: dict[str, str] = {
    "KOSPI": "Y",
    "KOSDAQ": "K",
    "KONEX": "N",
    "기타": "E",
}


def _to_corp_cls(markets: list[str]) -> list[str]:
    return [_MARKET_TO_CORP_CLS.get(m, m) for m in markets]


# -------------------- Latest trading date --------------------

def get_latest_trading_date() -> date | None:
    """Return the most recent date present in daily_prices.

    Used as a default 'as of' date for ranking queries when the caller
    doesn't specify one. Returns None if the table is empty (fresh DB).
    """
    with session_scope() as session:
        row = session.execute(text("SELECT MAX(date) FROM daily_prices")).first()
        return row[0] if row and row[0] else None


# -------------------- Ticker search --------------------

def search_tickers(
    keyword: str,
    markets: list[str] | None = None,
    include_delisted: bool = False,
    limit: int = 20,
) -> list[dict]:
    """Search tickers by symbol, name, sector, or industry.

    The keyword is matched as a case-insensitive substring against
    `symbol`, `name`, `sector`, and `industry`. This covers the common
    LLM patterns: "삼성", "반도체", "005930", "secondary battery".

    Args:
        keyword: Substring to match. Whitespace stripped.
        markets: Optional filter — e.g. ["KOSPI"] or ["KOSPI", "KOSDAQ"].
        include_delisted: If False (default), exclude delisted tickers.
        limit: Max rows returned. Hard-capped at 100 to protect context.

    Returns:
        List of dicts with keys: symbol, name, corp_cls, induty_code, delisted.
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []

    limit = max(1, min(int(limit), 100))

    where = ["(symbol ILIKE :kw OR name ILIKE :kw "
             "OR COALESCE(induty_code,'') ILIKE :kw)"]
    params: dict = {"kw": f"%{keyword}%"}

    if not include_delisted:
        where.append("delisted = FALSE")
    if markets:
        where.append("corp_cls = ANY(:corp_cls_values)")
        params["corp_cls_values"] = _to_corp_cls(markets)

    sql = text(
        "SELECT symbol, name, corp_cls, induty_code, delisted "
        "  FROM tickers "
        f" WHERE {' AND '.join(where)} "
        " ORDER BY (symbol = :exact) DESC, "
        "          (name = :exact)   DESC, "
        "          symbol "
        " LIMIT :limit"
    )
    params["exact"] = keyword
    params["limit"] = limit

    with session_scope() as session:
        rows = session.execute(sql, params).mappings().all()
        return [dict(r) for r in rows]


# -------------------- OHLCV history --------------------

def get_latest_price(symbol: str) -> dict | None:
    """Return the most recent daily row for one ticker, or None.

    Cheaper than get_price_history() when you only need the latest
    snapshot — uses an index-friendly ORDER BY date DESC LIMIT 1.
    """
    sql = text(
        "SELECT symbol, name, corp_cls, induty_code, date, "
        "       open, high, low, close, volume, value, "
        "       market_cap, shares, foreign_net, institution_net, "
        "       individual_net, per, pbr, dividend_yield "
        "  FROM v_daily_prices "
        " WHERE symbol = :sym "
        " ORDER BY date DESC LIMIT 1"
    )
    with session_scope() as session:
        row = session.execute(sql, {"sym": symbol}).mappings().first()
        return dict(row) if row else None


def get_price_history(
    symbol: str,
    start: date | None = None,
    end: date | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Return a single ticker's daily history within [start, end].

    Args:
        symbol: 6-digit Korean stock code (e.g. "005930").
        start: Inclusive lower bound. None = no lower bound.
        end: Inclusive upper bound. None = no upper bound.
        limit: Optional row cap. Results ordered by date ASC; the cap
            is applied AFTER ordering, so use it with `end=` to get
            the LAST N days (use a date filter, not a raw LIMIT, to
            get the FIRST N days).

    Returns:
        List of dicts ordered by date ascending (oldest first), each
        with the v_daily_prices columns: date, open/high/low/close,
        volume, value, market_cap, per, pbr, foreign_net, etc.
    """
    where = ["symbol = :sym"]
    params: dict = {"sym": symbol}
    if start is not None:
        where.append("date >= :start")
        params["start"] = start
    if end is not None:
        where.append("date <= :end")
        params["end"] = end

    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    sql = text(
        "SELECT date, open, high, low, close, volume, value, "
        "       market_cap, shares, foreign_net, institution_net, "
        "       individual_net, per, pbr, dividend_yield "
        "  FROM v_daily_prices "
        f" WHERE {' AND '.join(where)} "
        f" ORDER BY date ASC {limit_sql}"
    )
    with session_scope() as session:
        rows = session.execute(sql, params).mappings().all()
        return [dict(r) for r in rows]


# -------------------- Ranking: movers / volume / market cap --------------------

def _rank_query(
    metric_sql: str,
    on_date: date,
    markets: list[str] | None,
    descending: bool,
    limit: int,
    extra_where: str | None = None,
) -> list[dict]:
    """Internal helper for rank-by-metric queries.

    `metric_sql` is the expression to ORDER BY (and to expose as
    `metric`). It must be safe — only call sites in this module
    construct it from a fixed allow-list, never from user input.
    """
    where = ["date = :on_date"]
    params: dict = {"on_date": on_date}
    if markets:
        where.append("corp_cls = ANY(:corp_cls_values)")
        params["corp_cls_values"] = _to_corp_cls(markets)
    if extra_where:
        where.append(extra_where)
    direction = "DESC" if descending else "ASC"
    params["limit"] = max(1, min(int(limit), 100))

    sql = text(
        f"SELECT symbol, name, corp_cls, induty_code, "
        f"       open, high, low, close, volume, value, market_cap, "
        f"       per, pbr, dividend_yield, "
        f"       foreign_net, institution_net, individual_net, "
        f"       ({metric_sql}) AS metric "
        f"  FROM v_daily_prices "
        f" WHERE {' AND '.join(where)} "
        f" ORDER BY metric {direction} NULLS LAST "
        f" LIMIT :limit"
    )
    with session_scope() as session:
        rows = session.execute(sql, params).mappings().all()
        return [dict(r) for r in rows]


def top_by_return(
    on_date: date,
    markets: list[str] | None = None,
    direction: str = "gainers",
    limit: int = 10,
) -> list[dict]:
    """Top gainers or losers by intraday return (close/open - 1) for one day.

    Args:
        on_date: Trading date to rank on.
        markets: Optional — ["KOSPI"], ["KOSDAQ"], or both.
        direction: "gainers" (default) or "losers".
        limit: 1..100. Default 10.

    Returns rows with `metric` = decimal return (0.05 = +5%).
    """
    if direction not in ("gainers", "losers"):
        raise ValueError("direction must be 'gainers' or 'losers'")
    return _rank_query(
        metric_sql="CASE WHEN open > 0 THEN (close - open) / open END",
        on_date=on_date,
        markets=markets,
        descending=(direction == "gainers"),
        limit=limit,
        # Filter out tickers that didn't trade — open/close NULL or
        # zero would otherwise pollute the top of the list.
        extra_where="open IS NOT NULL AND close IS NOT NULL AND open > 0",
    )


def top_by_volume(
    on_date: date,
    markets: list[str] | None = None,
    by: str = "value",
    limit: int = 10,
) -> list[dict]:
    """Top traded names on a given day, by share volume or KRW value.

    Args:
        on_date: Trading date.
        markets: Optional market filter.
        by: "value" (KRW traded, default) or "volume" (share count).
        limit: 1..100.
    """
    if by not in ("value", "volume"):
        raise ValueError("by must be 'value' or 'volume'")
    return _rank_query(
        metric_sql=by,
        on_date=on_date,
        markets=markets,
        descending=True,
        limit=limit,
        extra_where=f"{by} IS NOT NULL",
    )


def top_by_market_cap(
    on_date: date,
    markets: list[str] | None = None,
    limit: int = 10,
) -> list[dict]:
    """Largest companies by market cap as of on_date."""
    return _rank_query(
        metric_sql="market_cap",
        on_date=on_date,
        markets=markets,
        descending=True,
        limit=limit,
        extra_where="market_cap IS NOT NULL AND market_cap > 0",
    )


# -------------------- Investor flows --------------------

def top_by_investor_net(
    on_date: date,
    investor: str,
    direction: str = "buy",
    markets: list[str] | None = None,
    limit: int = 10,
) -> list[dict]:
    """Top net-buyers / net-sellers by investor type for a day.

    Args:
        on_date: Trading date.
        investor: One of "foreign", "institution", "individual".
        direction: "buy" (top net buying, default) or "sell" (top net
            selling — i.e. most-negative net).
        markets: Optional market filter.
        limit: 1..100.

    Note: net values are in shares × price units as written by the
    pykrx collector (KRW-denominated net for institutions/foreigners
    in the standard pykrx schema). The MCP tool reports them verbatim.
    """
    col_map = {
        "foreign": "foreign_net",
        "institution": "institution_net",
        "individual": "individual_net",
    }
    if investor not in col_map:
        raise ValueError(
            "investor must be 'foreign', 'institution', or 'individual'"
        )
    if direction not in ("buy", "sell"):
        raise ValueError("direction must be 'buy' or 'sell'")

    col = col_map[investor]
    return _rank_query(
        metric_sql=col,
        on_date=on_date,
        markets=markets,
        descending=(direction == "buy"),
        limit=limit,
        extra_where=f"{col} IS NOT NULL",
    )


# -------------------- Financial snapshot --------------------

def get_financial_snapshot(symbol: str, on_date: date | None = None) -> dict | None:
    """Return PER/PBR/market_cap/dividend_yield for one ticker.

    Args:
        symbol: 6-digit code.
        on_date: As-of date. None = latest available row for that symbol.

    Returns:
        Dict with symbol, name, corp_cls, induty_code, date, close,
        market_cap, shares, per, pbr, dividend_yield. None if not found.
    """
    if on_date is None:
        sql = text(
            "SELECT symbol, name, corp_cls, induty_code, date, close, "
            "       market_cap, shares, per, pbr, dividend_yield "
            "  FROM v_daily_prices "
            " WHERE symbol = :sym "
            " ORDER BY date DESC LIMIT 1"
        )
        params = {"sym": symbol}
    else:
        sql = text(
            "SELECT symbol, name, corp_cls, induty_code, date, close, "
            "       market_cap, shares, per, pbr, dividend_yield "
            "  FROM v_daily_prices "
            " WHERE symbol = :sym AND date = :d"
        )
        params = {"sym": symbol, "d": on_date}

    with session_scope() as session:
        row = session.execute(sql, params).mappings().first()
        return dict(row) if row else None


def screen_by_valuation(
    on_date: date,
    per_max: float | None = None,
    pbr_max: float | None = None,
    dividend_min: float | None = None,
    market_cap_min: int | None = None,
    markets: list[str] | None = None,
    limit: int = 30,
) -> list[dict]:
    """Filter universe by simple valuation thresholds on a given day.

    All filters are AND-combined. Tickers with NULL values for a
    filtered-on column are excluded (so e.g. a `per_max=10` filter
    drops ETFs and loss-makers automatically).

    Args:
        on_date: As-of date.
        per_max: PER ≤ this. None = no filter.
        pbr_max: PBR ≤ this.
        dividend_min: dividend_yield ≥ this (in %).
        market_cap_min: market_cap ≥ this (in KRW).
        markets: Optional market filter.
        limit: 1..200.
    """
    where = ["date = :on_date"]
    params: dict = {"on_date": on_date}
    if per_max is not None:
        where.append("per IS NOT NULL AND per <= :per_max AND per > 0")
        params["per_max"] = per_max
    if pbr_max is not None:
        where.append("pbr IS NOT NULL AND pbr <= :pbr_max AND pbr > 0")
        params["pbr_max"] = pbr_max
    if dividend_min is not None:
        where.append("dividend_yield IS NOT NULL AND dividend_yield >= :div_min")
        params["div_min"] = dividend_min
    if market_cap_min is not None:
        where.append("market_cap IS NOT NULL AND market_cap >= :mc_min")
        params["mc_min"] = market_cap_min
    if markets:
        where.append("corp_cls = ANY(:corp_cls_values)")
        params["corp_cls_values"] = _to_corp_cls(markets)

    params["limit"] = max(1, min(int(limit), 200))
    sql = text(
        "SELECT symbol, name, corp_cls, induty_code, "
        "       close, market_cap, per, pbr, dividend_yield "
        "  FROM v_daily_prices "
        f" WHERE {' AND '.join(where)} "
        " ORDER BY market_cap DESC NULLS LAST "
        " LIMIT :limit"
    )
    with session_scope() as session:
        rows = session.execute(sql, params).mappings().all()
        return [dict(r) for r in rows]


# -------------------- DB summary --------------------

def get_db_summary() -> dict:
    """Return high-level database stats for orientation.

    Useful as a 'what's in here?' tool the LLM can call once at the
    start of a session.
    """
    with session_scope() as session:
        ticker_count = session.execute(text(
            "SELECT COUNT(*) FROM tickers WHERE delisted = FALSE"
        )).scalar() or 0
        delisted_count = session.execute(text(
            "SELECT COUNT(*) FROM tickers WHERE delisted = TRUE"
        )).scalar() or 0
        market_breakdown = session.execute(text(
            "SELECT market, COUNT(*) AS n FROM tickers "
            " WHERE delisted = FALSE GROUP BY market ORDER BY n DESC"
        )).mappings().all()
        date_range = session.execute(text(
            "SELECT MIN(date) AS min_date, MAX(date) AS max_date, "
            "       COUNT(*) AS n_rows FROM daily_prices"
        )).mappings().first()
        minute_range = session.execute(text(
            "SELECT MIN(ts) AS min_ts, MAX(ts) AS max_ts, "
            "       COUNT(*) AS n_rows FROM minute_prices"
        )).mappings().first()
    return {
        "active_tickers": int(ticker_count),
        "delisted_tickers": int(delisted_count),
        "markets": [dict(r) for r in market_breakdown],
        "daily_prices": dict(date_range) if date_range else {},
        "minute_prices": dict(minute_range) if minute_range else {},
    }
