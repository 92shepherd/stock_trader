"""Collect KOSPI + KOSDAQ ticker master from KRX.

Uses pykrx to fetch the full list, then upserts into the `tickers` table.
Handles listing/delisting changes on re-run.
"""
from __future__ import annotations

from datetime import date

from pykrx import stock
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.config import get_app_config
from src.db.connection import session_scope
from src.db.models import Ticker
from src.utils.logger import logger


def _fetch_market_tickers(market: str, ref_date: str) -> list[tuple[str, str]]:
    """Return [(symbol, name), ...] for one market on ref_date."""
    symbols = stock.get_market_ticker_list(ref_date, market=market)
    result = []
    for sym in symbols:
        name = stock.get_market_ticker_name(sym)
        if isinstance(name, str) and name:
            result.append((sym, name))
    return result


def _should_exclude(name: str, patterns: list[str]) -> bool:
    return any(p in name for p in patterns)


def collect_tickers(ref_date: date | None = None) -> dict[str, int]:
    """Fetch full KOSPI+KOSDAQ ticker list and upsert into DB.

    Returns a dict with counts: {inserted, updated, delisted_marked}
    """
    cfg = get_app_config()
    ref_date = ref_date or date.today()
    ref_s = ref_date.strftime("%Y%m%d")

    # 1) Fetch from KRX
    fresh: dict[str, tuple[str, str]] = {}  # symbol -> (name, market)
    for market in cfg.markets:
        logger.info(f"Fetching {market} tickers for {ref_s}...")
        rows = _fetch_market_tickers(market, ref_s)
        for sym, name in rows:
            if _should_exclude(name, cfg.exclude_patterns):
                continue
            fresh[sym] = (name, market)
        logger.info(f"  {market}: {len(rows)} tickers")

    logger.info(f"Total fresh tickers (after filters): {len(fresh)}")

    # 2) Upsert into DB
    inserted = 0
    updated = 0
    with session_scope() as session:
        existing_symbols = {
            s for (s,) in session.execute(select(Ticker.symbol)).all()
        }

        # Bulk upsert
        payload = [
            {
                "symbol": sym,
                "name": name,
                "market": market,
                "delisted": False,
                "delisted_date": None,
            }
            for sym, (name, market) in fresh.items()
        ]

        if payload:
            stmt = pg_insert(Ticker).values(payload)
            stmt = stmt.on_conflict_do_update(
                index_elements=["symbol"],
                set_={
                    "name": stmt.excluded.name,
                    "market": stmt.excluded.market,
                    "delisted": False,
                    "delisted_date": None,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            session.execute(stmt)

        inserted = len(fresh.keys() - existing_symbols)
        updated = len(fresh.keys() & existing_symbols)

        # 3) Mark delisted: existed before, missing now
        missing = existing_symbols - fresh.keys()
        delisted_marked = 0
        if missing:
            session.execute(
                update(Ticker)
                .where(Ticker.symbol.in_(missing), Ticker.delisted.is_(False))
                .values(delisted=True, delisted_date=ref_date)
            )
            delisted_marked = len(missing)

    logger.success(
        f"Tickers updated — inserted: {inserted}, "
        f"updated: {updated}, newly delisted: {delisted_marked}"
    )
    return {
        "inserted": inserted,
        "updated": updated,
        "delisted_marked": delisted_marked,
    }


if __name__ == "__main__":
    collect_tickers()
