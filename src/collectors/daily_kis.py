"""Daily price collector using KIS Open API.

Strategy:
    Like daily_fdr (symbol-iterating, NOT date-iterating), because KIS's
    inquire-daily-itemchartprice is a per-symbol multi-date endpoint:

        inquire_daily_itemchartprice(symbol, start, end)
            → up to 100 trading days, in one call

    For ~2,600 KOSPI+KOSDAQ symbols × 400-day backfill
    (~265 trading days → 3 calls per adjusted/raw axis):

        2,600 symbols × 2 axes × 3 windows = ~15,600 daily-chart calls
        2,600 symbols × 1 (today only)     = ~2,600 inquire-price calls
        --------------------------------------------------------------
        ~18,200 calls total

    With KIS's 20 req/s ceiling on real / 5 req/s on paper (we use 75%
    headroom by default), a full backfill takes roughly 20–30 minutes
    on real, 1–2 hours on paper. Resume via collection_log makes
    re-runs cheap.

What this module does:
    1. For each symbol, walk windows of MAX_DAYS_PER_CALL until the
       requested [start, end] is fully covered.
    2. Per symbol, call inquire-daily-itemchartprice TWICE:
         - adjusted=True  → daily_prices (수정주가)
         - adjusted=False → daily_prices_raw (원주가)
       Yes, this doubles the call budget. The two tables are distinct
       sources of truth (메모리 정책: keep tables normalized, don't
       merge adj/raw into one row).
    3. Once the symbol's window backfill finishes, call inquire-price
       ONCE to get today's snapshot (시총/PER/PBR/외국인순매수 ...) and
       merge it into the most-recent daily_prices row whose date == end.
       Past-date market_cap/PER/PBR/foreign_net stays NULL because KIS
       doesn't provide history for those fields.
    4. log_collection(target_date=end, symbol=symbol, ...) on success
       so daily_fdr-style resume (`get_completed_symbols_in_range`)
       works against the same `daily_kis` collector name.

Things this module does NOT do:
    - institution_net / individual_net: KIS inquire-price doesn't
      expose these. Only foreign_net (frgn_ntby_qty) is available
      from this endpoint. The other two stay NULL.
    - dividend_yield: not in inquire-price response either.
    - Multi-process parallelism: KIS rate limits are per-key, so
      parallelizing wouldn't help and would just risk throttling.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from src.config import get_app_config
from src.db.repositories import (
    get_active_tickers,
    get_completed_symbols_in_range,
    log_collection,
    upsert_daily_prices,
    upsert_daily_prices_raw,
)
from src.kis.auth import get_kis_auth
from src.kis.quotations import (
    MAX_DAYS_PER_CALL,
    KISQuotations,
    KISQuotationsError,
    get_kis_quotations,
)
from src.utils.logger import logger

COLLECTOR_NAME = "daily_kis"

# Per-mode default request delay (seconds between API calls).
# 75% of KIS's published rate limit gives headroom for token refresh
# and other concurrent traffic on the same app key.
#   real:  20 rps → 15 rps target → ~67 ms/call
#   paper:  5 rps →  4 rps target → ~250 ms/call
_DEFAULT_DELAY_BY_MODE = {
    "real": 0.07,
    "paper": 0.25,
}

# After this many consecutive symbol failures, abort the run.
# Likely indicates a token/rate-limit/host-down problem rather than
# isolated bad symbols.
_CIRCUIT_BREAKER_FAILURES = 10


# ---------------------------------------------------------------------------
# Helpers — string→numeric coercion (KIS returns everything as strings)
# ---------------------------------------------------------------------------


def _to_int(v: Any) -> int | None:
    """Coerce a KIS string field to int, returning None on empty/invalid.

    KIS uses '' or '0' inconsistently for "no value"; we treat both
    permissively. Sign-prefixed strings ('-1234') parse correctly.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s or s == "-":
        return None
    try:
        return int(float(s))  # float() tolerates '1.0' which int() doesn't
    except (TypeError, ValueError):
        return None


def _to_decimal(v: Any) -> Decimal | None:
    """Coerce to Decimal for NUMERIC columns (price, ratios)."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s == "-":
        return None
    try:
        return Decimal(s)
    except (TypeError, ValueError, InvalidOperation):
        return None


def _parse_kis_date(s: str) -> date | None:
    """Parse 'YYYYMMDD' → date. Returns None on bad input."""
    if not s or len(s) != 8:
        return None
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Window splitting — KIS allows ≤100 days per call
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Window:
    start: date
    end: date

    def fmt_start(self) -> str:
        return self.start.strftime("%Y%m%d")

    def fmt_end(self) -> str:
        return self.end.strftime("%Y%m%d")


def _split_windows(start: date, end: date, days_per_call: int) -> list[_Window]:
    """Split [start, end] into chunks of <= days_per_call calendar days.

    KIS counts in trading days, not calendar days, but using calendar
    days is a safe over-approximation: at most 5/7 of calendar days are
    trading days, so a 100-calendar-day window is always ≤ 100 trading
    days, never exceeding KIS's per-call cap.

    Iterates from `end` backward (most recent first) so we can short-
    circuit a run if the most recent slice already says "no data" (e.g.
    delisted symbols).
    """
    if end < start:
        return []
    windows: list[_Window] = []
    cursor_end = end
    while cursor_end >= start:
        cursor_start = max(start, cursor_end - timedelta(days=days_per_call - 1))
        windows.append(_Window(start=cursor_start, end=cursor_end))
        if cursor_start <= start:
            break
        cursor_end = cursor_start - timedelta(days=1)
    return windows


# ---------------------------------------------------------------------------
# Row builders — KIS payload → DataFrame row
# ---------------------------------------------------------------------------

# Map output2 row from inquire-daily-itemchartprice → our column shape.
# Both adjusted and raw responses share the same field names.
def _bars_to_records(
    symbol: str,
    bars: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Transform KIS daily-chart `output2` items into DAILY_RAW_COLUMNS shape.

    Same schema works for both adjusted (→ daily_prices subset) and raw
    (→ daily_prices_raw): symbol/date/OHLC/volume/value. Fundamentals
    are layered on separately by `_apply_today_snapshot`.
    """
    out: list[dict[str, Any]] = []
    for b in bars:
        d = _parse_kis_date(b.get("stck_bsop_date", ""))
        if d is None:
            continue
        close = _to_decimal(b.get("stck_clpr"))
        # Drop rows where close is missing or zero (holidays / halts).
        if close is None or close <= 0:
            continue
        out.append(
            {
                "symbol": symbol,
                "date": d,
                "open": _to_decimal(b.get("stck_oprc")),
                "high": _to_decimal(b.get("stck_hgpr")),
                "low": _to_decimal(b.get("stck_lwpr")),
                "close": close,
                "volume": _to_int(b.get("acml_vol")),
                "value": _to_int(b.get("acml_tr_pbmn")),
            }
        )
    return out


def _apply_today_snapshot(
    df: pd.DataFrame,
    symbol: str,
    end: date,
    snapshot: dict[str, Any],
) -> pd.DataFrame:
    """Layer inquire-price snapshot fields onto the (symbol, end) row of df.

    KIS only provides current values — there's no historical
    market_cap/PER/PBR/foreign_net. So we attach the snapshot to a
    single row: the one whose date matches the requested `end` (which
    is typically today). For backfills with end < today, this means the
    snapshot is merged into a slightly-stale date — that's acceptable
    because (a) historical 시총/PER are unrecoverable anyway, and
    (b) day-to-day changes in these fields are small.

    hts_avls comes from KIS in 억원 (100M KRW); we convert to KRW (BIGINT)
    to match the schema (`market_cap BIGINT`).
    """
    if df.empty or not snapshot:
        return df

    # Find the row to update — match by date == end. If not present
    # (e.g. end is a holiday), no-op rather than fabricating a row.
    mask = (df["symbol"] == symbol) & (df["date"] == end)
    if not mask.any():
        return df

    # Fundamentals — all string, may be empty/zero on some symbols.
    market_cap_eok = _to_int(snapshot.get("hts_avls"))  # 억원 → KRW
    market_cap = market_cap_eok * 100_000_000 if market_cap_eok is not None else None

    updates = {
        "market_cap": market_cap,
        "shares": _to_int(snapshot.get("lstn_stcn")),
        "per": _to_decimal(snapshot.get("per")),
        "pbr": _to_decimal(snapshot.get("pbr")),
        "foreign_net": _to_int(snapshot.get("frgn_ntby_qty")),
        # institution_net / individual_net: KIS inquire-price doesn't
        # expose these. Left NULL by upsert_daily_prices.
        # dividend_yield: not in inquire-price either.
    }
    for col, val in updates.items():
        if col not in df.columns:
            df[col] = None
        df.loc[mask, col] = val
    return df


# ---------------------------------------------------------------------------
# Per-symbol fetch
# ---------------------------------------------------------------------------


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _safe_chart(
    quotations: KISQuotations,
    *,
    symbol: str,
    start: str,
    end: str,
    adjusted: bool,
) -> list[dict[str, Any]]:
    """tenacity-wrapped daily-chart call. Retries TRANSPORT errors only.

    Business errors (KISQuotationsError) propagate immediately — they
    indicate a request problem (bad symbol / off-hours block) that
    retrying doesn't help.
    """
    _, output2 = quotations.inquire_daily_itemchartprice(
        symbol=symbol, start=start, end=end, adjusted=adjusted,
    )
    return output2


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _safe_snapshot(quotations: KISQuotations, symbol: str) -> dict[str, Any]:
    return quotations.inquire_price(symbol=symbol)


def collect_one_symbol(
    quotations: KISQuotations,
    symbol: str,
    start: date,
    end: date,
    *,
    request_delay: float,
    fetch_snapshot: bool = True,
) -> tuple[int, int]:
    """Backfill one symbol's daily prices over [start, end].

    Returns (rows_adj, rows_raw) — affected row counts in
    daily_prices and daily_prices_raw respectively.

    Raises:
        KISQuotationsError: business error after retries (bad symbol etc.)
        Exception: transport/db errors after retries.
    """
    windows = _split_windows(start, end, MAX_DAYS_PER_CALL)
    if not windows:
        return 0, 0

    adj_records: list[dict[str, Any]] = []
    raw_records: list[dict[str, Any]] = []

    # Walk windows; sleep request_delay between every API call.
    for w in windows:
        # 1) adjusted (수정주가) — for daily_prices
        try:
            adj_bars = _safe_chart(
                quotations, symbol=symbol,
                start=w.fmt_start(), end=w.fmt_end(), adjusted=True,
            )
            adj_records.extend(_bars_to_records(symbol, adj_bars))
        except KISQuotationsError as e:
            logger.warning(f"  {symbol} adjusted {w.start}→{w.end} business error: {e}")
            # Non-recoverable per-window business error: skip this window
            # but continue with raw + remaining windows. Empty windows
            # are normal (newly listed symbols, delisting gaps).
        time.sleep(request_delay)

        # 2) raw (원주가) — for daily_prices_raw
        try:
            raw_bars = _safe_chart(
                quotations, symbol=symbol,
                start=w.fmt_start(), end=w.fmt_end(), adjusted=False,
            )
            raw_records.extend(_bars_to_records(symbol, raw_bars))
        except KISQuotationsError as e:
            logger.warning(f"  {symbol} raw {w.start}→{w.end} business error: {e}")
        time.sleep(request_delay)

    # If both tables came back empty, treat as a no-data symbol (delisted,
    # too new, suspended) and return 0/0 — caller will log 'skipped'.
    if not adj_records and not raw_records:
        return 0, 0

    adj_df = pd.DataFrame(adj_records)
    raw_df = pd.DataFrame(raw_records)

    # 3) today's snapshot — only if we have at least one adjusted row.
    if fetch_snapshot and not adj_df.empty:
        try:
            snapshot = _safe_snapshot(quotations, symbol)
            adj_df = _apply_today_snapshot(adj_df, symbol, end, snapshot)
        except KISQuotationsError as e:
            logger.warning(f"  {symbol} inquire-price snapshot failed: {e}")
        time.sleep(request_delay)

    rows_adj = upsert_daily_prices(adj_df) if not adj_df.empty else 0
    rows_raw = upsert_daily_prices_raw(raw_df) if not raw_df.empty else 0
    return rows_adj, rows_raw


# ---------------------------------------------------------------------------
# Backfill driver
# ---------------------------------------------------------------------------


def backfill_symbols(
    symbols: list[str] | None = None,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    days: int | None = None,
    skip_done: bool = True,
    request_delay: float | None = None,
    fetch_snapshot: bool = True,
) -> None:
    """Backfill daily prices via KIS API for a set of symbols.

    Args:
        symbols: list of 6-digit codes. None = all active tickers in
            configured markets.
        start_date / end_date / days: same semantics as daily_pykrx /
            daily_fdr.
        skip_done: skip symbols whose log already shows success at
            target_date == end_date with rows_inserted > 0.
        request_delay: seconds between API calls. None → mode default
            (real: 0.07s, paper: 0.25s).
        fetch_snapshot: if True (default), also call inquire-price per
            symbol to populate market_cap/per/pbr/foreign_net on the
            end_date row. Set False when you only need OHLCV (faster).

    Usage:
        backfill_symbols(days=400)
        backfill_symbols(symbols=["005930", "000660"], days=30)
        backfill_symbols(start_date=date(2025,1,1))
    """
    cfg = get_app_config()
    end_date = end_date or date.today()
    if start_date is None:
        days = days or cfg.collection.daily.backfill_days
        start_date = end_date - timedelta(days=days)

    if symbols is None:
        tickers = get_active_tickers(markets=cfg.markets)
        symbols = [t.symbol for t in tickers]
    if not symbols:
        logger.warning("No symbols to backfill. Populate `tickers` table first.")
        return

    # Determine delay from auth mode if not specified.
    if request_delay is None:
        mode = get_kis_auth().mode
        request_delay = _DEFAULT_DELAY_BY_MODE.get(mode, 0.25)
        logger.info(f"Using mode-default request_delay={request_delay}s ({mode})")

    quotations = get_kis_quotations()

    # Resume support — skip symbols already done for this exact end_date.
    if skip_done:
        done = get_completed_symbols_in_range(COLLECTOR_NAME, start_date, end_date)
        before = len(symbols)
        symbols = [s for s in symbols if s not in done]
        logger.info(
            f"Resume: {len(done)} symbols already done at {end_date}; "
            f"{len(symbols)}/{before} remaining."
        )

    if not symbols:
        logger.success("Nothing to backfill — all symbols already done.")
        return

    logger.info(
        f"KIS daily backfill: {len(symbols)} symbols, "
        f"{start_date} → {end_date}, snapshot={fetch_snapshot}"
    )

    total_adj = 0
    total_raw = 0
    consecutive_failures = 0

    for symbol in tqdm(symbols, desc="daily_kis backfill"):
        t0 = time.time()
        try:
            rows_adj, rows_raw = collect_one_symbol(
                quotations, symbol, start_date, end_date,
                request_delay=request_delay,
                fetch_snapshot=fetch_snapshot,
            )
            dur = int((time.time() - t0) * 1000)
            if rows_adj == 0 and rows_raw == 0:
                log_collection(
                    COLLECTOR_NAME, end_date, "skipped",
                    symbol=symbol, duration_ms=dur,
                )
            else:
                # Log the SUM as rows_inserted for resume bookkeeping.
                # The breakdown is in the logs; what matters for resume
                # is "this symbol+end_date pair has rows_inserted > 0".
                log_collection(
                    COLLECTOR_NAME, end_date, "success",
                    symbol=symbol,
                    rows_inserted=rows_adj + rows_raw,
                    duration_ms=dur,
                )
                total_adj += rows_adj
                total_raw += rows_raw
            consecutive_failures = 0
        except Exception as e:
            dur = int((time.time() - t0) * 1000)
            logger.error(f"  {symbol} failed: {e}")
            log_collection(
                COLLECTOR_NAME, end_date, "failed",
                symbol=symbol, error_message=str(e)[:500],
                duration_ms=dur,
            )
            consecutive_failures += 1
            if consecutive_failures >= _CIRCUIT_BREAKER_FAILURES:
                logger.error(
                    f"Aborting: {consecutive_failures} consecutive failures. "
                    "Likely a KIS token/rate-limit/host issue. "
                    "Re-run later to resume."
                )
                break

    logger.success(
        f"KIS backfill done. daily_prices rows: {total_adj:,}; "
        f"daily_prices_raw rows: {total_raw:,}"
    )


if __name__ == "__main__":
    backfill_symbols()
