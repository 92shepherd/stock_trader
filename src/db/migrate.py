"""Simple SQL migration runner.

Reads .sql files from migrations/ in alphabetical order and executes them.
Tracks applied migrations in schema_migrations table.
"""
from __future__ import annotations

from pathlib import Path

from psycopg import RawCursor
from sqlalchemy import text

from src.db.connection import get_engine
from src.utils.logger import logger

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"


_ENSURE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    VARCHAR(200) PRIMARY KEY,
    applied_at  TIMESTAMPTZ DEFAULT NOW()
);
"""


def _applied_set(conn) -> set[str]:
    rows = conn.execute(text("SELECT filename FROM schema_migrations")).fetchall()
    return {r[0] for r in rows}


def run_migrations() -> None:
    """Apply any pending migrations.

    Why RawCursor (psycopg3):
        psycopg3 by default treats '%' in SQL text as the start of a
        parameter placeholder (%s, %(name)s). That breaks migration files
        that contain '%' in comments (e.g. "-- ratio in % units") or in
        string literals, even though no parameters are being bound.

        RawCursor uses PostgreSQL native placeholders ($1, $2) instead,
        so '%' in the SQL text is left untouched. Since our migrations
        never bind parameters, this is exactly the right tool.

        Reference:
            https://www.psycopg.org/psycopg3/docs/advanced/cursors.html
            #raw-query-cursors
    """
    engine = get_engine()
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        logger.warning(f"No migration files found in {MIGRATIONS_DIR}")
        return

    with engine.begin() as conn:
        conn.execute(text(_ENSURE_TABLE_SQL))
        applied = _applied_set(conn)

    for f in files:
        if f.name in applied:
            logger.info(f"  skip (already applied): {f.name}")
            continue

        logger.info(f"  applying: {f.name}")
        sql = f.read_text(encoding="utf-8")

        # Each migration runs in its own transaction; if it fails, we stop.
        with engine.begin() as conn:
            # Borrow the underlying psycopg3 connection from SQLAlchemy and
            # build a RawCursor on it so '%' in the SQL is not parsed.
            raw_conn = conn.connection.dbapi_connection
            with RawCursor(raw_conn) as cur:
                cur.execute(sql)
            conn.execute(
                text("INSERT INTO schema_migrations(filename) VALUES (:f)"),
                {"f": f.name},
            )
        logger.success(f"  done: {f.name}")

    logger.success("All migrations applied.")


if __name__ == "__main__":
    run_migrations()
