"""CLI entry: `python -m src.api`.

Starts the FastAPI app via uvicorn with sensible defaults from .env:
  - API_HOST (default: 127.0.0.1)
  - API_PORT (default: 8765)
  - LOG_LEVEL (default: INFO, lowercased for uvicorn)

For development reload, prefer:
    uvicorn src.api.app:app --reload --host 127.0.0.1 --port 8765

This module is the production entry, not the dev entry. It does NOT
enable --reload because a) reloads invalidate the in-process scheduler
state, and b) running collectors mid-reload would orphan threads.

CLI flags supported (override .env):
    --host HOST           bind address
    --port PORT           bind port
    --log-level LEVEL     uvicorn log level (debug|info|warning|error)
"""
from __future__ import annotations

# .env first \u2014 same reason as app.py. load_dotenv() is idempotent so
# this is safe even though app.py does it too.
# override=True — see app.py 의 동일 호출에 달린 주석 참고. 세션의
# (혹은 빈 값의) 기존 환경변수보다 .env 파일을 우선.
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)

import argparse  # noqa: E402
import os  # noqa: E402

import uvicorn  # noqa: E402


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the stock_trader REST API server (uvicorn).",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("API_HOST", "127.0.0.1"),
        help="Bind address (default: env API_HOST, fallback 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_env_int("API_PORT", 8765),
        help="Bind port (default: env API_PORT, fallback 8765)",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "info").lower(),
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        help="uvicorn log level (default: env LOG_LEVEL, fallback info)",
    )
    args = parser.parse_args()

    uvicorn.run(
        # Use the import string, not the `app` object, so uvicorn can
        # cleanly handle worker processes and signal forwarding.
        "src.api.app:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        # Single worker on purpose: the in-process scheduler and the
        # per-collector locks assume one process. Multi-worker would
        # break that contract immediately.
        workers=1,
        # No reload in prod \u2014 see module docstring.
        reload=False,
    )


if __name__ == "__main__":
    main()
