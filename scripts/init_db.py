"""One-time DB initialization.

Runs after `docker compose up -d`:
    python -m scripts.init_db
"""
from __future__ import annotations

from sqlalchemy import text

from src.db.connection import get_engine
from src.db.migrate import run_migrations
from src.utils.logger import logger


def check_connection() -> None:
    engine = get_engine()
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version()")).scalar()
        ts_ver = conn.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
        ).scalar()
    logger.success(f"Connected: {version}")
    logger.success(f"TimescaleDB: {ts_ver}")


def main() -> None:
    logger.info("=== Initializing database ===")
    check_connection()
    run_migrations()
    logger.success("Database ready.")


if __name__ == "__main__":
    main()
