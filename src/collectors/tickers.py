"""Collect KOSPI + KOSDAQ ticker master.

Two sources are available:
  - pykrx  : `collect_tickers()`      — official KRX data, symbol+name only
  - fdr    : `collect_tickers_fdr()`  — includes sector/industry/listing_date

Both upsert into the same `tickers` table and handle listing/delisting on
re-run. Use whichever fits your pipeline; if both are run, the later one
wins on conflicting fields (ON CONFLICT DO UPDATE).
"""
from __future__ import annotations

from datetime import date

import FinanceDataReader as fdr
import pandas as pd
from pykrx import stock
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.config import get_app_config
from src.db.connection import session_scope
from src.db.models import Ticker
from src.utils.logger import logger


def _should_exclude(name: str, patterns: list[str]) -> bool:
    return any(p in name for p in patterns)


def _upsert_and_mark_delisted(
    payload: list[dict],
    ref_date: date,
) -> dict[str, int]:
    """Shared upsert path for both pykrx- and fdr-based collectors.

    - Inserts or updates every row in `payload`.
    - Any symbol currently in `tickers` but NOT in the fresh payload is
      marked delisted (delisted=TRUE, delisted_date=ref_date).
    - Returns counts: {inserted, updated, delisted_marked}.

    Important: `payload` rows must include at minimum {symbol, name, market}.
    Optional keys: sector, industry, listing_date.
    """
    fresh_symbols = {p["symbol"] for p in payload}

    with session_scope() as session:
        existing_symbols = {
            s for (s,) in session.execute(select(Ticker.symbol)).all()
        }

        # Normalize — make sure every row has the same key set so the bulk
        # INSERT statement's column list is consistent.
        for p in payload:
            p.setdefault("sector", None)
            p.setdefault("industry", None)
            p.setdefault("listing_date", None)
            p["delisted"] = False
            p["delisted_date"] = None

        if payload:
            stmt = pg_insert(Ticker).values(payload)
            # Only overwrite columns we actually have fresh data for.
            # updated_at uses the server default on INSERT; on UPDATE we
            # let PG pick up excluded.updated_at (which falls back to
            # the default NOW() on the server side during the upsert).
            update_cols = {
                "name": stmt.excluded.name,
                "market": stmt.excluded.market,
                "sector": stmt.excluded.sector,
                "industry": stmt.excluded.industry,
                "listing_date": stmt.excluded.listing_date,
                "delisted": False,
                "delisted_date": None,
                "updated_at": stmt.excluded.updated_at,
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=["symbol"], set_=update_cols,
            )
            session.execute(stmt)

        inserted = len(fresh_symbols - existing_symbols)
        updated = len(fresh_symbols & existing_symbols)

        # Mark delisted: existed before, missing now
        missing = existing_symbols - fresh_symbols
        delisted_marked = 0
        if missing:
            session.execute(
                update(Ticker)
                .where(Ticker.symbol.in_(missing), Ticker.delisted.is_(False))
                .values(delisted=True, delisted_date=ref_date)
            )
            delisted_marked = len(missing)

    return {
        "inserted": inserted,
        "updated": updated,
        "delisted_marked": delisted_marked,
    }


# ==================================================================
# pykrx-based collector (original)
# ==================================================================

def _fetch_market_tickers_pykrx(
    market: str, ref_date: str,
) -> list[tuple[str, str]]:
    """Return [(symbol, name), ...] for one market on ref_date."""
    symbols = stock.get_market_ticker_list(ref_date, market=market)
    result = []
    for sym in symbols:
        name = stock.get_market_ticker_name(sym)
        if isinstance(name, str) and name:
            result.append((sym, name))
    return result


def collect_tickers(ref_date: date | None = None) -> dict[str, int]:
    """Fetch full KOSPI+KOSDAQ ticker list via pykrx and upsert into DB.

    pykrx exposes symbol+name only, so sector/industry/listing_date are
    left NULL. If you want those filled, use `collect_tickers_fdr(desc=True)`
    instead.

    Returns a dict with counts: {inserted, updated, delisted_marked}
    """
    cfg = get_app_config()
    ref_date = ref_date or date.today()
    ref_s = ref_date.strftime("%Y%m%d")

    fresh: dict[str, tuple[str, str]] = {}  # symbol -> (name, market)
    for market in cfg.markets:
        logger.info(f"[pykrx] Fetching {market} tickers for {ref_s}...")
        rows = _fetch_market_tickers_pykrx(market, ref_s)
        for sym, name in rows:
            if _should_exclude(name, cfg.exclude_patterns):
                continue
            fresh[sym] = (name, market)
        logger.info(f"  {market}: {len(rows)} tickers")

    logger.info(f"[pykrx] Total fresh tickers (after filters): {len(fresh)}")

    payload = [
        {"symbol": sym, "name": name, "market": market}
        for sym, (name, market) in fresh.items()
    ]
    result = _upsert_and_mark_delisted(payload, ref_date)

    logger.success(
        f"[pykrx] Tickers updated — inserted: {result['inserted']}, "
        f"updated: {result['updated']}, "
        f"newly delisted: {result['delisted_marked']}"
    )
    return result


# ==================================================================
# FinanceDataReader-based collector
# ==================================================================

def _parse_listing_date(v) -> date | None:
    """fdr returns ListingDate as a pandas Timestamp or string. Normalize."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        ts = pd.to_datetime(v, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.date()
    except Exception:
        return None


def _fetch_market_tickers_fdr(market: str, desc: bool) -> pd.DataFrame:
    """Return a DataFrame of one market's tickers from fdr.

    When `desc=True`, uses the '-DESC' variant which includes Sector,
    Industry, and ListingDate. This costs an extra HTTP call vs the plain
    variant but takes only a few seconds.
    """
    listing_key = f"{market}-DESC" if desc else market
    df = fdr.StockListing(listing_key)

    if df is None or df.empty:
        logger.warning(f"[fdr] StockListing('{listing_key}') returned empty")
        return pd.DataFrame()

    # fdr column names vary a bit across versions; be defensive.
    # Expected columns (post-0.9.6x, KRX-DESC):
    #   Code, Name, Market, Sector, Industry, ListingDate, ...
    # Plain KOSPI/KOSDAQ typically returns: Code, Name, (Market,) ...
    rename = {}
    if "Code" in df.columns:
        rename["Code"] = "symbol"
    if "Name" in df.columns:
        rename["Name"] = "name"
    if "Sector" in df.columns:
        rename["Sector"] = "sector"
    if "Industry" in df.columns:
        rename["Industry"] = "industry"
    if "ListingDate" in df.columns:
        rename["ListingDate"] = "listing_date"
    df = df.rename(columns=rename)

    # Plain variants don't include Market — inject it ourselves.
    df["market"] = market

    # Keep only the columns we need; ensure they all exist.
    for col in ("symbol", "name", "sector", "industry", "listing_date"):
        if col not in df.columns:
            df[col] = None

    df = df[["symbol", "name", "market", "sector", "industry", "listing_date"]]

    # Sanity: drop rows with no symbol or no name
    df = df[df["symbol"].notna() & df["name"].notna()].copy()
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df["name"] = df["name"].astype(str)

    return df


def collect_tickers_fdr(
    ref_date: date | None = None,
    desc: bool = True,
) -> dict[str, int]:
    """Fetch full KOSPI+KOSDAQ ticker list via FinanceDataReader and upsert.

    Args:
        ref_date: date to stamp on newly-delisted rows. Defaults to today.
        desc: if True (default), use the '-DESC' variant which includes
            sector/industry/listing_date. Set False for a slightly faster
            fetch if you only care about symbol+name.

    Returns a dict with counts: {inserted, updated, delisted_marked}

    Why you'd use this over `collect_tickers()`:
      - You want sector/industry/listing_date populated in `tickers`.
      - pykrx is down/throttled and you need a fallback.
      - You're running the FDR price collector and prefer one source end-to-end.
    """
    cfg = get_app_config()
    ref_date = ref_date or date.today()

    frames: list[pd.DataFrame] = []
    for market in cfg.markets:
        logger.info(
            f"[fdr] Fetching {market} tickers "
            f"({'with desc' if desc else 'symbol+name only'})..."
        )
        df = _fetch_market_tickers_fdr(market, desc=desc)
        if df.empty:
            continue
        logger.info(f"  {market}: {len(df)} tickers")
        frames.append(df)

    if not frames:
        logger.error("[fdr] No ticker data fetched from any market")
        return {"inserted": 0, "updated": 0, "delisted_marked": 0}

    merged = pd.concat(frames, ignore_index=True)

    # Apply exclude_patterns from config (same semantics as pykrx path)
    if cfg.exclude_patterns:
        mask = merged["name"].apply(
            lambda n: not _should_exclude(n, cfg.exclude_patterns)
        )
        before = len(merged)
        merged = merged[mask].copy()
        logger.info(
            f"[fdr] exclude_patterns filtered {before - len(merged)} rows"
        )

    # Same-symbol duplicates across markets shouldn't happen for KRX but be safe
    merged = merged.drop_duplicates(subset=["symbol"], keep="first")

    logger.info(f"[fdr] Total fresh tickers (after filters): {len(merged)}")

    # Build payload for the shared upsert helper
    payload = []
    for row in merged.itertuples(index=False):
        payload.append({
            "symbol": row.symbol,
            "name": row.name,
            "market": row.market,
            "sector": row.sector if pd.notna(row.sector) else None,
            "industry": row.industry if pd.notna(row.industry) else None,
            "listing_date": _parse_listing_date(row.listing_date),
        })

    result = _upsert_and_mark_delisted(payload, ref_date)

    logger.success(
        f"[fdr] Tickers updated — inserted: {result['inserted']}, "
        f"updated: {result['updated']}, "
        f"newly delisted: {result['delisted_marked']}"
    )
    return result


if __name__ == "__main__":
    # Default to the pykrx collector for backwards compatibility.
    # To run the fdr variant:  python -c "from src.collectors.tickers import collect_tickers_fdr; collect_tickers_fdr()"
    collect_tickers()
