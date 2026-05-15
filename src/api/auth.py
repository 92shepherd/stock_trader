"""API key authentication via the `X-API-Key` header.

Single shared key, loaded from .env (STOCK_TRADER_API_KEY). This is a
local trigger API — there is no user model and no per-key permissions.
The key exists to prevent the local network or a misconfigured proxy
from firing collectors against this process by accident.

Usage on a route:

    from fastapi import Depends
    from src.api.auth import require_api_key

    @router.post("/collect/...", dependencies=[Depends(require_api_key)])
    async def trigger_xyz(...): ...

Failure modes:
    - STOCK_TRADER_API_KEY empty/unset \u2192 401 with a clear message so the
      operator knows to populate .env. (We refuse to "default allow"
      because that hides a misconfiguration.)
    - Header missing or wrong \u2192 401.

We use a constant-time comparison (`secrets.compare_digest`) to avoid
leaking key length / prefix via timing, even though this API is
loopback-only by default.
"""
from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException, status


# Header name. Lowercase per HTTP spec; FastAPI matches case-insensitively.
_API_KEY_HEADER = "X-API-Key"


def _expected_key() -> str:
    """Read the expected key from .env at call time (not import time)."""
    return os.getenv("STOCK_TRADER_API_KEY", "").strip()


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias=_API_KEY_HEADER),
) -> None:
    """FastAPI dependency that 401s unless `X-API-Key` matches .env.

    Returning None on success is intentional: callers wire this up via
    `dependencies=[Depends(require_api_key)]`, not by consuming a return
    value.
    """
    expected = _expected_key()
    if not expected:
        # Refuse to accept any request when the key isn't configured.
        # This is loud-on-purpose: the alternative is silently accepting
        # everything in a misconfigured deployment.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "STOCK_TRADER_API_KEY is not set on the server. Configure "
                "it in .env before issuing requests."
            ),
        )
    if x_api_key is None or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing or invalid {_API_KEY_HEADER} header.",
            headers={"WWW-Authenticate": _API_KEY_HEADER},
        )


__all__ = ["require_api_key"]
