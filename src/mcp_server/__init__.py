"""MCP server exposing stock_trader's TimescaleDB as queryable tools.

This package wraps the existing repository layer (`src.db.repositories`)
in MCP tool functions so that an LLM (Claude Desktop, Claude Code, or
any MCP-compatible client) can query the collected stock data without
writing SQL directly.

Design principles:
    - Read-only by default. No tool in this package writes to the DB.
    - Reuse existing repository functions where they fit; add new
      read-only helpers in `queries.py` only when the existing API
      isn't shaped for the question (e.g. top-movers, ranking).
    - Each tool returns plain dicts/lists (JSON-serializable) — no
      pandas DataFrames or SQLAlchemy objects leak across the MCP
      boundary.
    - Tool docstrings are the LLM's only spec — keep them precise.

Entry points:
    - `python -m src.mcp_server`           → stdio (Claude Desktop / Code)
    - `python -m src.mcp_server --http`    → HTTP/SSE (remote clients)
"""
from __future__ import annotations

from src.mcp_server.server import mcp

__all__ = ["mcp"]
