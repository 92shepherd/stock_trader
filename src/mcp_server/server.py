"""FastMCP server instance.

We construct the server here (separate from __main__) so that:
    - Tests can `from src.mcp_server.server import mcp` and inspect
      registered tools without entering a transport loop.
    - The HTTP and stdio entry points share the same instance.

Tools are registered eagerly on import. They're lightweight closures
that delegate to `queries.py` — no DB connection is opened until a
tool is actually invoked.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from src.mcp_server.tools import register_all
from src.utils.logger import logger

# Server name surfaces in the MCP client UI (Claude Desktop / Code).
mcp = FastMCP("stock-trader")

register_all(mcp)

logger.info("MCP server 'stock-trader' initialized with all tool groups")

__all__ = ["mcp"]
