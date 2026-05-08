"""Investor flow tools: foreign / institution / individual net buying."""
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
    d = _parse_date(s)
    if d is not None:
        return d
    latest = queries.get_latest_trading_date()
    return latest if latest else date(1970, 1, 1)


def register(mcp) -> None:
    @mcp.tool()
    def top_investor_flow(
        investor: str,
        on_date: str | None = None,
        direction: str = "buy",
        markets: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Top net-buyers or net-sellers by investor type for one day.

        Investor categories follow pykrx's standard breakdown:
            - 'foreign'     외국인 net flow (foreign_net column)
            - 'institution' 기관 net flow (institution_net column)
            - 'individual'  개인 net flow (individual_net column)

        Args:
            investor: 'foreign' | 'institution' | 'individual'.
            on_date: 'YYYY-MM-DD'. Defaults to latest available date.
            direction: 'buy' (top net buying — most positive flow,
                default) or 'sell' (top net selling — most negative flow).
            markets: Optional market filter.
            limit: 1..100, default 10.

        Each returned row contains the OHLCV/financials snapshot plus a
        `metric` field with the net flow (units as written by pykrx).
        Rows with NULL flow are excluded.
        """
        rows = queries.top_by_investor_net(
            on_date=_resolve_date(on_date),
            investor=investor,
            direction=direction,
            markets=markets,
            limit=limit,
        )
        return jsonify_rows(rows)
