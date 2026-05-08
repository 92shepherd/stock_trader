"""JSON-serialization helpers for MCP tool returns.

The MCP protocol expects JSON-serializable payloads. Our queries
return rows containing `Decimal` (NUMERIC columns) and `date` /
`datetime` values, neither of which json.dumps handles natively.

We coerce them at the boundary so individual tools don't need to
remember to do it.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _coerce(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        # Decimals come from NUMERIC columns. Convert to float for
        # readability — we accept the small precision loss because the
        # consumer is an LLM, not a downstream calc engine.
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def jsonify_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: _coerce(v) for k, v in row.items()}


def jsonify_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [jsonify_row(r) for r in rows]
