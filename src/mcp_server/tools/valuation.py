"""Valuation tools: PER/PBR/market-cap lookup and screening."""
from __future__ import annotations

from datetime import date, datetime

from src.mcp_server import queries
from src.mcp_server.serializers import jsonify_row, jsonify_rows


def _parse_date(s: str | None) -> date | None:
    if s is None or s == "":
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return datetime.fromisoformat(s).date()


def _resolve_date(s: str | None) -> date:
    d = _parse_date(s)
    if d is not None:
        return d
    latest = queries.get_latest_trading_date()
    return latest if latest else date(1970, 1, 1)


def register(mcp) -> None:
    @mcp.tool()
    def get_valuation(symbol: str, on_date: str | None = None) -> dict | None:
        """Get PER, PBR, market cap, and dividend yield for one ticker.

        Args:
            symbol: 6-digit Korean stock code.
            on_date: 'YYYY-MM-DD'. If omitted, returns the latest row
                available for that symbol.

        Returns a dict with symbol, name, market, sector, industry, date,
        close, market_cap, shares, per, pbr, dividend_yield. Null if
        the symbol has no rows.

        Note: PER/PBR/dividend_yield come from pykrx's daily snapshot.
        Tickers that haven't been collected via pykrx (only via FDR)
        will have NULL values for these.
        """
        snap = queries.get_financial_snapshot(
            symbol=symbol,
            on_date=_parse_date(on_date),
        )
        return jsonify_row(snap) if snap else None

    @mcp.tool()
    def screen_stocks(
        on_date: str | None = None,
        per_max: float | None = None,
        pbr_max: float | None = None,
        dividend_min: float | None = None,
        market_cap_min: int | None = None,
        markets: list[str] | None = None,
        limit: int = 30,
    ) -> list[dict]:
        """Filter the universe by simple valuation thresholds.

        All thresholds are AND-combined. A NULL value in a filtered
        column excludes the row, so e.g. `per_max=10` automatically
        drops loss-makers (negative/NULL PER) and ETFs.

        Args:
            on_date: 'YYYY-MM-DD'. Defaults to latest available date.
            per_max: Keep tickers with PER ≤ this. None = no filter.
            pbr_max: Keep tickers with PBR ≤ this.
            dividend_min: Keep tickers with dividend_yield ≥ this (%).
            market_cap_min: Keep tickers with market_cap ≥ this (KRW).
            markets: Optional market filter.
            limit: 1..200, default 30. Results sorted by market_cap DESC.

        Typical patterns:
            - "저PER 우량주": per_max=10, market_cap_min=1_000_000_000_000
            - "고배당 KOSPI": dividend_min=4.0, markets=['KOSPI']
        """
        rows = queries.screen_by_valuation(
            on_date=_resolve_date(on_date),
            per_max=per_max,
            pbr_max=pbr_max,
            dividend_min=dividend_min,
            market_cap_min=market_cap_min,
            markets=markets,
            limit=limit,
        )
        return jsonify_rows(rows)
