"""Entry point for running the MCP server.

Usage:
    # stdio (Claude Desktop / Claude Code config)
    python -m src.mcp_server

    # HTTP / SSE (remote clients)
    python -m src.mcp_server --http
    python -m src.mcp_server --http --host 0.0.0.0 --port 8765

    # Streamable HTTP (newer transport, recommended for remote)
    python -m src.mcp_server --streamable-http

The .env file is loaded explicitly here (not just relied on via
pydantic-settings) so DB credentials are guaranteed to be available
before the first tool call — same pattern as the collection pipelines.
"""
from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from src.utils.logger import logger

# Load .env BEFORE importing the server module — the server's import
# chain reaches pydantic-settings, which reads env at module-import
# time. This mirrors the pattern used in src/pipelines/*.
load_dotenv()

from src.mcp_server.server import mcp  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m src.mcp_server",
        description="stock_trader MCP server — query Korean stock data via MCP.",
    )
    transport = p.add_mutually_exclusive_group()
    transport.add_argument(
        "--stdio",
        action="store_true",
        help="Use stdio transport (default; for Claude Desktop / Code).",
    )
    transport.add_argument(
        "--http",
        action="store_true",
        help="Use SSE-over-HTTP transport (legacy MCP HTTP).",
    )
    transport.add_argument(
        "--streamable-http",
        action="store_true",
        dest="streamable_http",
        help="Use streamable-HTTP transport (newer; recommended for remote).",
    )
    p.add_argument("--host", default="127.0.0.1", help="HTTP bind host.")
    p.add_argument("--port", type=int, default=8765, help="HTTP bind port.")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    if args.http:
        transport = "sse"
    elif args.streamable_http:
        transport = "streamable-http"
    else:
        transport = "stdio"

    if transport in ("sse", "streamable-http"):
        # FastMCP reads host/port from its `settings` object. Set them
        # before run() so the bind happens on the requested address.
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        logger.info(
            f"Starting MCP server on {transport} at {args.host}:{args.port}"
        )
    else:
        logger.info("Starting MCP server on stdio transport")

    try:
        mcp.run(transport=transport)
    except KeyboardInterrupt:
        logger.info("MCP server stopped by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
