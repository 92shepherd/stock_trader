"""Verify collected data.

Runs a suite of sanity-check queries against the DB and prints results.

Usage:
    python -m scripts.verify_data
"""
from __future__ import annotations

from sqlalchemy import text

from src.db.connection import get_engine
from src.utils.logger import logger


QUERIES: dict[str, str] = {
    "ticker_count": """
        SELECT market, COUNT(*) AS n
        FROM tickers
        WHERE delisted = FALSE
        GROUP BY market
        ORDER BY market
    """,
    "daily_date_range": """
        SELECT MIN(date) AS min_date,
               MAX(date) AS max_date,
               COUNT(DISTINCT date) AS trading_days,
               COUNT(*) AS total_rows,
               COUNT(DISTINCT symbol) AS n_symbols
        FROM daily_prices
    """,
    "daily_per_day_counts_recent": """
        SELECT date, COUNT(*) AS n_symbols
        FROM daily_prices
        WHERE date > (SELECT MAX(date) - INTERVAL '10 days' FROM daily_prices)
        GROUP BY date
        ORDER BY date DESC
    """,
    "daily_per_market_recent": """
        SELECT d.date, t.market, COUNT(*) AS n
        FROM daily_prices d
        JOIN tickers t USING (symbol)
        WHERE d.date > (SELECT MAX(date) - INTERVAL '5 days' FROM daily_prices)
        GROUP BY d.date, t.market
        ORDER BY d.date DESC, t.market
    """,
    "samsung_recent_30d": """
        SELECT date, name, open, high, low, close, volume
        FROM v_daily_prices
        WHERE symbol = '005930'
        ORDER BY date DESC
        LIMIT 30
    """,
    "top_market_cap_latest": """
        SELECT date, symbol, name, market, close, market_cap
        FROM v_daily_prices
        WHERE date = (SELECT MAX(date) FROM daily_prices)
          AND market_cap IS NOT NULL
        ORDER BY market_cap DESC
        LIMIT 10
    """,
    "collection_failures": """
        SELECT target_date, collector, status, error_message
        FROM collection_log
        WHERE status <> 'success'
        ORDER BY target_date DESC
        LIMIT 20
    """,
    "hypertable_info": """
        SELECT hypertable_name,
               num_chunks,
               compression_enabled
        FROM timescaledb_information.hypertables
    """,
    "chunk_compression_status": """
        SELECT hypertable_name,
               COUNT(*) AS total_chunks,
               COUNT(*) FILTER (WHERE is_compressed) AS compressed_chunks
        FROM timescaledb_information.chunks
        GROUP BY hypertable_name
    """,
}


def run() -> None:
    engine = get_engine()
    with engine.connect() as conn:
        for name, sql in QUERIES.items():
            logger.info(f"\n=== {name} ===")
            try:
                result = conn.execute(text(sql))
                cols = result.keys()
                rows = result.fetchall()
                if not rows:
                    print("  (no rows)")
                    continue
                # Pretty-print
                print("  " + " | ".join(str(c) for c in cols))
                print("  " + "-" * 60)
                for r in rows:
                    print("  " + " | ".join(str(v) for v in r))
            except Exception as e:
                logger.error(f"  query failed: {e}")


if __name__ == "__main__":
    run()
