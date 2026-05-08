"""MCP tool implementations, grouped by domain.

Each module exposes a `register(mcp)` function that attaches its
tools to the shared FastMCP instance. The split is purely
organizational — at runtime all tools end up on the same server.

Modules:
    basic     — ticker search, OHLCV history, latest snapshot
    analysis  — top movers, top volume, top market cap
    investors — foreign / institution / individual net flows
    valuation — PER/PBR/시총 lookup and screening
    meta      — DB summary, latest trading date
"""
from __future__ import annotations

from src.mcp_server.tools import analysis, basic, investors, meta, valuation


def register_all(mcp) -> None:
    """Attach every tool group to the given FastMCP instance."""
    meta.register(mcp)
    basic.register(mcp)
    analysis.register(mcp)
    investors.register(mcp)
    valuation.register(mcp)


__all__ = ["register_all"]
