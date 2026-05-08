"""Analysis tools: ranking the universe by movement, volume, market cap."""
from __future__ import annotations

from datetime import date, datetime

from src.mcp_server import queries
from src.mcp_server.serializers import jsonify_rows


def _parse_date(s: str | None) -> date | None:
    if s is None or s == "":
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return datetime.fromisoformat(s).date()


def _resolve_date(s: str | None) -> date:
    """Return parsed date, falling back to the latest collected date."""
    d = _parse_date(s)
    if d is not None:
        return d
    latest = queries.get_latest_trading_date()
    if latest is None:
        # No data at all — let the downstream query return [].
        # Return a far-past date so any equality filter matches nothing.
        return date(1970, 1, 1)
    return latest


def register(mcp) -> None:
    @mcp.tool()
    def top_movers(
        on_date: str | None = None,
        direction: str = "gainers",
        markets: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Top gainers or losers by intraday return for a given trading day.

        Return is computed as (close - open) / open. Tickers that didn't
        trade (open or close NULL, or open == 0) are excluded.

        Args:
            on_date: 'YYYY-MM-DD'. If omitted, uses the latest available
                trading date in the database.
            direction: 'gainers' (largest positive return, default) or
                'losers' (most negative).
            markets: Optional ['KOSPI'], ['KOSDAQ'], or both. None = all.
            limit: 1..100, default 10.

        Each row includes the OHLCV row plus a `metric` field with the
        decimal return (0.05 means +5%).
        """
        rows = queries.top_by_return(
            on_date=_resolve_date(on_date),
            markets=markets,
            direction=direction,
            limit=limit,
        )
        return jsonify_rows(rows)

    @mcp.tool()
    def top_volume(
        on_date: str | None = None,
        by: str = "value",
        markets: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Most-traded names on a given day.

        Args:
            on_date: 'YYYY-MM-DD'. Defaults to latest available date.
            by: 'value' (KRW traded, default) or 'volume' (share count).
                Use 'value' for cross-cap comparison; 'volume' favours
                low-priced high-share-count names.
            markets: Optional market filter.
            limit: 1..100.
        """
        rows = queries.top_by_volume(
            on_date=_resolve_date(on_date),
            markets=markets,
            by=by,
            limit=limit,
        )
        return jsonify_rows(rows)

    @mcp.tool()
    def top_market_cap(
        on_date: str | None = None,
        markets: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Largest companies by market capitalization on a given day.

        Args:
            on_date: 'YYYY-MM-DD'. Defaults to latest available date.
            markets: Optional market filter. Useful for KOSDAQ-only or
                KOSPI-only rankings.
            limit: 1..100.
        """
        rows = queries.top_by_market_cap(
            on_date=_resolve_date(on_date),
            markets=markets,
            limit=limit,
        )
        return jsonify_rows(rows)
