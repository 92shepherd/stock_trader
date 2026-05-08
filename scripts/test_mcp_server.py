"""Smoke-test the MCP server tools without going through a real client.

Exercises every registered tool with realistic arguments and prints
shapes/counts. Run this after schema changes or after adding new
tools to catch breakages early.

Usage:
    python -m scripts.test_mcp_server
"""
from __future__ import annotations

import json

from dotenv import load_dotenv

load_dotenv()

from src.mcp_server import queries  # noqa: E402
from src.mcp_server.server import mcp  # noqa: E402
from src.utils.logger import logger  # noqa: E402


def _print(label: str, value) -> None:
    """Compact printer that truncates long lists/dicts."""
    if isinstance(value, list):
        logger.info(f"{label}: {len(value)} rows")
        if value:
            logger.info(f"  first row keys: {list(value[0].keys())}")
            logger.info(f"  sample: {json.dumps(value[0], default=str, ensure_ascii=False)[:200]}")
    elif isinstance(value, dict):
        logger.info(f"{label}: dict with keys {list(value.keys())}")
        logger.info(f"  sample: {json.dumps(value, default=str, ensure_ascii=False)[:200]}")
    else:
        logger.info(f"{label}: {value}")


def main() -> None:
    # 1. List registered tools
    tool_names = sorted(t.name for t in mcp._tool_manager.list_tools())
    logger.info(f"Registered tools ({len(tool_names)}): {tool_names}")

    # 2. Pick a recent date for ranking queries
    latest = queries.get_latest_trading_date()
    if latest is None:
        logger.warning("daily_prices is empty — only meta tools will exercise")
    else:
        logger.info(f"Latest trading date: {latest}")

    # 3. Direct query-layer smoke tests (bypassing the MCP wrapper but
    #    covering every SQL path the tools depend on).
    _print("db_summary", queries.get_db_summary())

    _print(
        "search_tickers('삼성')",
        queries.search_tickers("삼성", limit=5),
    )
    _print(
        "search_tickers('005930')",
        queries.search_tickers("005930", limit=3),
    )

    # Pick a symbol that probably exists, fall back gracefully.
    samples = queries.search_tickers("005930", limit=1) \
        or queries.search_tickers("삼성", limit=1)
    if samples:
        sym = samples[0]["symbol"]
        _print(
            f"get_latest_price({sym})",
            queries.get_latest_price(sym),
        )
        _print(
            f"get_price_history({sym}, last 5)",
            queries.get_price_history(sym, limit=5)[-5:],
        )

    if latest is not None:
        _print(
            "top_by_return(gainers)",
            queries.top_by_return(latest, direction="gainers", limit=3),
        )
        _print(
            "top_by_return(losers, KOSPI)",
            queries.top_by_return(latest, markets=["KOSPI"], direction="losers", limit=3),
        )
        _print(
            "top_by_volume(value)",
            queries.top_by_volume(latest, by="value", limit=3),
        )
        _print(
            "top_by_market_cap",
            queries.top_by_market_cap(latest, limit=3),
        )
        _print(
            "top_by_investor_net(foreign, buy)",
            queries.top_by_investor_net(latest, "foreign", direction="buy", limit=3),
        )
        _print(
            "screen_by_valuation(per_max=10, pbr_max=1)",
            queries.screen_by_valuation(latest, per_max=10, pbr_max=1, limit=5),
        )

    logger.info("All smoke tests completed without exceptions")


if __name__ == "__main__":
    main()
