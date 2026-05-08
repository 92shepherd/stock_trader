"""Basic lookup tools: ticker search and OHLCV history."""
from __future__ import annotations

from datetime import date, datetime

from src.mcp_server import queries
from src.mcp_server.serializers import jsonify_rows


def _parse_date(s: str | None) -> date | None:
    """Accept 'YYYY-MM-DD' (and ISO datetime) from the LLM. None passthrough."""
    if s is None or s == "":
        return None
    # date.fromisoformat accepts both 'YYYY-MM-DD' and the full ISO
    # form on Python 3.11+, but be lenient.
    try:
        return date.fromisoformat(s)
    except ValueError:
        return datetime.fromisoformat(s).date()


def register(mcp) -> None:
    @mcp.tool()
    def search_tickers(
        keyword: str,
        markets: list[str] | None = None,
        include_delisted: bool = False,
        limit: int = 20,
    ) -> list[dict]:
        """Search Korean stock tickers by symbol, name, sector, or industry.

        Matches the keyword as a case-insensitive substring against the
        symbol code (e.g. '005930'), the company name (e.g. '삼성전자'),
        the sector, and the industry. Exact symbol or name matches are
        returned first.

        Args:
            keyword: Substring to search for. Required.
            markets: Optional list — e.g. ['KOSPI'], ['KOSDAQ'], or both.
                None = all markets.
            include_delisted: If true, include delisted tickers. Default false.
            limit: Max rows to return. 1..100, default 20.

        Returns a list of {symbol, name, market, sector, industry,
        listing_date, delisted}.
        """
        rows = queries.search_tickers(
            keyword=keyword,
            markets=markets,
            include_delisted=include_delisted,
            limit=limit,
        )
        return jsonify_rows(rows)

    @mcp.tool()
    def get_price_history(
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Get a single ticker's daily OHLCV history within a date range.

        Returns full v_daily_prices columns: date, open, high, low,
        close, volume, value (KRW traded), market_cap, shares,
        foreign_net, institution_net, individual_net, per, pbr,
        dividend_yield. Rows are ordered by date ascending (oldest
        first).

        Args:
            symbol: 6-digit Korean stock code, e.g. '005930' for 삼성전자.
            start: Inclusive start date as 'YYYY-MM-DD'. Optional.
            end: Inclusive end date as 'YYYY-MM-DD'. Optional.
            limit: Optional cap on row count. Applied AFTER date sort,
                so to get the most-recent N days, prefer setting `end`
                rather than relying on limit alone.
        """
        rows = queries.get_price_history(
            symbol=symbol,
            start=_parse_date(start),
            end=_parse_date(end),
            limit=limit,
        )
        return jsonify_rows(rows)

    @mcp.tool()
    def get_latest_quote(symbol: str) -> dict | None:
        """Get the most recent daily snapshot for one ticker.

        Convenience wrapper for 'what's the last close of <symbol>'.
        Returns the same fields as get_price_history but only the most
        recent row, or null if the symbol is unknown / has no prices.
        """
        from src.mcp_server.serializers import jsonify_row

        row = queries.get_latest_price(symbol)
        return jsonify_row(row) if row else None
