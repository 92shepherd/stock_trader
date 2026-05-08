"""Meta tools: orientation queries about the database itself."""
from __future__ import annotations

from src.mcp_server import queries
from src.mcp_server.serializers import jsonify_row


def register(mcp) -> None:
    @mcp.tool()
    def db_summary() -> dict:
        """Get a high-level summary of what's in the stock database.

        Useful as the first call in a new session to learn:
          - How many active vs delisted Korean tickers exist
          - Per-market ticker counts (KOSPI / KOSDAQ / ...)
          - Date range of daily price data
          - Whether minute-bar data is present and its range

        Returns a dict with keys:
            active_tickers (int), delisted_tickers (int),
            markets (list of {market, n}),
            daily_prices ({min_date, max_date, n_rows}),
            minute_prices ({min_ts, max_ts, n_rows}).
        """
        return jsonify_row(queries.get_db_summary())

    @mcp.tool()
    def latest_trading_date() -> str | None:
        """Get the most recent date present in the daily_prices table.

        Use this as a default 'as of' date for ranking/screening tools
        when the user doesn't specify one. Returns an ISO-format date
        string (YYYY-MM-DD), or null if no data has been collected.
        """
        d = queries.get_latest_trading_date()
        return d.isoformat() if d else None
