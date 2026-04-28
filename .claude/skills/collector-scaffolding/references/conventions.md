# Collector Conventions — Concrete Snippets

This document supplements `SKILL.md` with the actual code patterns extracted
from `daily_pykrx.py`, `daily_fdr.py`, `tickers.py`, and `repositories.py`.
When generating a new collector, match these snippets in spirit; adapt names
and details.

---

## 1. Module header (every collector)

```python
"""<One-line description>.

Strategy:
    <Explain WHY the iteration shape was chosen, not just what it does.
    Mention upstream API constraints, performance trade-offs, and what
    happens when things go wrong. See daily_pykrx.py module docstring
    for the quality bar.>

What this module does NOT provide:
    <List columns/data this collector leaves NULL, so future collectors
    can fill them via ON CONFLICT DO UPDATE.>
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import <upstream_lib>  # e.g. FinanceDataReader, pykrx, OpenDartReader, httpx
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from src.config import get_app_config
from src.db.repositories import (
    # whichever helpers are needed
    log_collection,
    upsert_<table>,
    # one of:
    get_completed_symbols_in_range,  # symbol-iterating
    get_missing_dates,                # date-iterating
)
from src.utils.logger import logger

COLLECTOR_NAME = "<snake_case_name>"
```

`COLLECTOR_NAME` MUST match the filename stem and the value passed to
`log_collection(...)`. The pipeline CLI module name follows the pattern
`collect_<COLLECTOR_NAME>.py`.

---

## 2. The retry-wrapped fetch (every collector)

```python
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _safe_fetch(<args>) -> <return_type>:
    """Call upstream with retry. <Lib> occasionally throws on transient
    network/parse errors — exponential backoff usually clears it."""
    return <upstream_call>(<args>)
```

Every external HTTP / network call gets this wrapper. Never call the upstream
library directly from `collect_one_*` — always go through `_safe_*`.

---

## 3. Defensive normalize (every collector)

```python
_EXPECTED_COLS = {"<col1>", "<col2>", ...}  # subset that MUST be present

def _normalize(df: pd.DataFrame, <key>: str) -> pd.DataFrame:
    """Shape upstream response to match <table> column conventions.

    Returns empty DF on malformed/empty input rather than raising, so the
    caller's loop can continue.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    if not (set(df.columns) & _EXPECTED_COLS):
        logger.warning(
            f"  {<key>} response has unexpected columns: "
            f"{list(df.columns)} — skipping"
        )
        return pd.DataFrame()

    df = df.rename(columns={...})
    df = df.reset_index().rename(columns={"<index_name>": "<col_name>"})
    df["symbol"] = <key>  # if applicable

    keep = ["symbol", "date", ...]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].copy()

    # Type coercions
    df["date"] = pd.to_datetime(df["date"]).dt.date

    # Drop bad rows (zero/null close, etc.)
    df = df[df["<key_metric>"].notna() & (df["<key_metric>"] > 0)]

    return df
```

The defensive column check exists because upstreams (pykrx, fdr) silently
return malformed DataFrames during outages. We log + return empty rather
than crashing the whole backfill.

---

## 4a. Symbol-iterating: `collect_one_symbol` + `backfill_symbols` + `backfill_active_universe`

This is the `daily_fdr` pattern. Use when one upstream call = one symbol's
data over a range.

```python
def collect_one_symbol(
    symbol: str,
    start_date: date,
    end_date: date,
) -> int:
    """Fetch one symbol's data for [start_date, end_date] and upsert.

    Returns the number of rows upserted. Raises on persistent fetch failure
    (after retries exhausted).
    """
    df = _safe_fetch(symbol, start_date, end_date)
    df = _normalize(df, symbol)
    if df.empty:
        return 0
    return upsert_<table>(df)


def backfill_symbols(
    symbols: list[str],
    start_date: date | None = None,
    end_date: date | None = None,
    days: int | None = None,
    skip_done: bool = True,
    consecutive_fail_limit: int = 20,
) -> dict[str, int]:
    cfg = get_app_config()
    end_date = end_date or date.today()
    if start_date is None:
        days = days or cfg.collection.<grain>.backfill_days
        start_date = end_date - timedelta(days=days)

    if not symbols:
        logger.warning("backfill_symbols called with empty symbol list")
        return {"ok": 0, "failed": 0, "empty": 0, "skipped": 0, "total_rows": 0}

    # Resume support
    skipped_done = 0
    if skip_done:
        already = get_completed_symbols_in_range(
            COLLECTOR_NAME, start_date, end_date
        )
        if already:
            before = len(symbols)
            symbols = [s for s in symbols if s not in already]
            skipped_done = before - len(symbols)
            logger.info(
                f"Resume: {skipped_done} symbol(s) already completed for "
                f"end_date={end_date}, {len(symbols)} remaining"
            )

    if not symbols:
        logger.success("Nothing to backfill — every symbol already done.")
        return {"ok": 0, "failed": 0, "empty": 0,
                "skipped": skipped_done, "total_rows": 0}

    logger.info(
        f"<COLLECTOR> backfill: {len(symbols)} symbol(s), "
        f"{start_date} → {end_date}"
    )

    ok = failed = empty = total_rows = 0
    consecutive_failures = 0

    for sym in tqdm(symbols, desc="<descriptive label>"):
        t0 = time.time()
        try:
            rows = collect_one_symbol(sym, start_date, end_date)
            dur = int((time.time() - t0) * 1000)
            if rows == 0:
                empty += 1
                log_collection(
                    COLLECTOR_NAME, end_date, "skipped",
                    symbol=sym, duration_ms=dur,
                )
            else:
                ok += 1
                total_rows += rows
                log_collection(
                    COLLECTOR_NAME, end_date, "success",
                    symbol=sym, rows_inserted=rows, duration_ms=dur,
                )
            consecutive_failures = 0
        except Exception as e:
            failed += 1
            consecutive_failures += 1
            dur = int((time.time() - t0) * 1000)
            logger.error(f"  {sym}: {e}")
            log_collection(
                COLLECTOR_NAME, end_date, "failed",
                symbol=sym, error_message=str(e)[:500], duration_ms=dur,
            )
            if (
                consecutive_fail_limit > 0
                and consecutive_failures >= consecutive_fail_limit
            ):
                logger.error(
                    f"Aborting: {consecutive_failures} consecutive failures. "
                    "Likely a network/source issue. Re-run to resume."
                )
                break
        time.sleep(cfg.collection.<grain>.request_delay)

    logger.success(
        f"<COLLECTOR> backfill done — ok: {ok}, failed: {failed}, "
        f"empty: {empty}, skipped(done): {skipped_done}, "
        f"total rows: {total_rows:,}"
    )
    return {"ok": ok, "failed": failed, "empty": empty,
            "skipped": skipped_done, "total_rows": total_rows}


def backfill_active_universe(
    start_date: date | None = None,
    end_date: date | None = None,
    days: int | None = None,
    markets: list[str] | None = None,
    skip_done: bool = True,
) -> dict[str, int]:
    cfg = get_app_config()
    tickers = get_active_tickers(markets or cfg.markets)
    symbols = [t.symbol for t in tickers]
    if not symbols:
        logger.warning(
            "Active universe is empty — populate `tickers` first."
        )
        return {"ok": 0, "failed": 0, "empty": 0, "skipped": 0, "total_rows": 0}
    logger.info(f"Active universe: {len(symbols)} ticker(s)")
    return backfill_symbols(
        symbols, start_date=start_date, end_date=end_date,
        days=days, skip_done=skip_done,
    )
```

Critical points:
- Resume key is `target_date == end_date` AND `symbol == sym`. The repository
  helper `get_completed_symbols_in_range` enforces this contract.
- `skipped` status (no rows) is NOT counted as success → next resume retries it.
- Circuit breaker triggers BEFORE the `time.sleep` so we exit immediately on
  cascading failures.

---

## 4b. Date-iterating: `collect_one_period` + `backfill`

This is the `daily_pykrx` pattern. Use when one upstream call = all symbols'
data for a single date.

```python
def collect_one_period(target: date) -> int:
    """Collect one period across all configured markets. Returns row count.

    Errors in individual markets are logged but do NOT abort other markets.
    Only raises if ALL markets fail (transient outage).
    """
    cfg = get_app_config()
    frames: list[pd.DataFrame] = []
    errors: list[tuple[str, Exception]] = []

    for market in cfg.markets:
        try:
            df = _fetch_one_period(target, market)
            if not df.empty:
                frames.append(df)
            time.sleep(cfg.collection.<grain>.request_delay)
        except Exception as e:
            logger.error(f"  {target} [{market}] failed: {e}")
            errors.append((market, e))

    if errors and len(errors) == len(cfg.markets):
        markets_str = ", ".join(m for m, _ in errors)
        raise RuntimeError(
            f"All markets failed for {target} ({markets_str}): {errors[0][1]}"
        )

    if not frames:
        return 0

    merged = pd.concat(frames, ignore_index=True)
    merged = merged[merged["<key>"].notna() & (merged["<key>"] > 0)]
    if merged.empty:
        return 0
    return upsert_<table>(merged)


def backfill(
    start_date: date | None = None,
    end_date: date | None = None,
    days: int | None = None,
    skip_done: bool = True,
) -> None:
    cfg = get_app_config()
    end_date = end_date or date.today()
    if start_date is None:
        days = days or cfg.collection.<grain>.backfill_days
        start_date = end_date - timedelta(days=days)

    logger.info(f"Backfilling: {start_date} → {end_date}")

    if skip_done:
        targets = get_missing_dates(COLLECTOR_NAME, start_date, end_date)
        logger.info(f"Candidate periods (not yet done): {len(targets)}")
    else:
        targets = []
        d = start_date
        while d <= end_date:
            if d.weekday() < 5:
                targets.append(d)
            d += timedelta(days=1)
        logger.info(f"Candidate periods: {len(targets)}")

    if not targets:
        logger.success("Nothing to backfill.")
        return

    total_rows = 0
    consecutive_failures = 0
    for target in tqdm(targets, desc="<descriptive label>"):
        t0 = time.time()
        try:
            rows = collect_one_period(target)
            dur = int((time.time() - t0) * 1000)
            if rows == 0:
                log_collection(
                    COLLECTOR_NAME, target, "skipped", duration_ms=dur,
                )
            else:
                log_collection(
                    COLLECTOR_NAME, target, "success",
                    rows_inserted=rows, duration_ms=dur,
                )
                total_rows += rows
            consecutive_failures = 0
        except Exception as e:
            dur = int((time.time() - t0) * 1000)
            logger.error(f"Failed on {target}: {e}")
            log_collection(
                COLLECTOR_NAME, target, "failed",
                error_message=str(e)[:500], duration_ms=dur,
            )
            consecutive_failures += 1
            if consecutive_failures >= 10:
                logger.error(
                    f"Aborting: {consecutive_failures} consecutive failures. "
                    "Re-run later to resume."
                )
                break

    logger.success(f"Backfill done. Total rows upserted: {total_rows:,}")
```

---

## 5. Repository upsert (every new table)

This pattern is non-negotiable for time-series tables. Mirror
`upsert_daily_prices` exactly:

```python
<TABLE>_COLUMNS = ["symbol", "date", ...]  # full column list

def upsert_<table>(df: pd.DataFrame) -> int:
    """Bulk upsert <table> via COPY + ON CONFLICT.

    Expects df columns to be a subset of <TABLE>_COLUMNS.
    Missing columns will be NULLed out.
    Returns number of rows inserted/updated.
    """
    if df.empty:
        return 0

    for col in <TABLE>_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[<TABLE>_COLUMNS].copy()

    buf = StringIO()
    df.to_csv(buf, index=False, header=False, sep="\t", na_rep="\\N")
    buf.seek(0)

    col_list = ", ".join(<TABLE>_COLUMNS)
    update_cols = [c for c in <TABLE>_COLUMNS if c not in (<pk_cols>)]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    with raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE tmp_<table> "
                "(LIKE <table> INCLUDING DEFAULTS) "
                "ON COMMIT DROP"
            )
            with cur.copy(
                f"COPY tmp_<table> ({col_list}) FROM STDIN "
                f"WITH (FORMAT CSV, DELIMITER E'\\t', NULL '\\N')"
            ) as copy:
                copy.write(buf.read())
            cur.execute(
                f"INSERT INTO <table> ({col_list}) "
                f"SELECT {col_list} FROM tmp_<table> "
                f"ON CONFLICT ({pk_csv}) DO UPDATE SET {update_clause}"
            )
            affected = cur.rowcount
    return affected
```

Why COPY + ON CONFLICT and not ORM bulk insert?
- COPY is ~10x faster for time-series.
- TEMP table + INSERT...SELECT keeps the upsert atomic per batch.
- `INCLUDING DEFAULTS` makes the temp table mirror the real schema exactly.

---

## 6. Resume helper (symbol-iterating only)

For symbol-iterating collectors, add a custom helper if your resume key isn't
just `(collector, end_date, symbol)`. For DART quarterly, for example:

```python
def get_completed_symbol_periods(
    collector: str,
    period_marker: <type>,  # e.g. "2024Q3"
) -> set[str]:
    """Return symbols that successfully completed `period_marker` for
    `collector`. Used to make per-symbol period backfills resumable."""
    with session_scope() as session:
        stmt = text("""
            SELECT DISTINCT symbol
              FROM collection_log
             WHERE collector = :col
               AND status    = 'success'
               AND target_date = :marker  -- repurpose: store as date if needed
               AND symbol IS NOT NULL
               AND rows_inserted > 0
        """)
        return {r[0] for r in session.execute(
            stmt, {"col": collector, "marker": period_marker}
        ).all()}
```

For date-iterating collectors, REUSE `get_missing_dates` from repositories.py.
Don't write a parallel implementation.

---

## 7. Pipeline CLI structure

Mirror `collect_daily_fdr.py` exactly. The flag set is non-negotiable:

```python
"""CLI entry point for <description>.

Usage:
    python -m src.pipelines.collect_<n>
    python -m src.pipelines.collect_<n> --start 2024-01-01 --end 2025-12-31
    python -m src.pipelines.collect_<n> --days 400 --no-skip-done
    python -m src.pipelines.collect_<n> --symbols 005930 000660
"""
from __future__ import annotations

# Load .env BEFORE importing modules that read os.environ at import time.
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import argparse
from datetime import date

from src.collectors.<n> import (
    backfill_symbols,
    backfill_active_universe,  # if applicable
)
from src.collectors.tickers import collect_tickers_fdr  # or collect_tickers
from src.utils.logger import logger


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> None:
    parser = argparse.ArgumentParser(description="<COLLECTOR> pipeline")
    parser.add_argument("--start", type=_parse_date)
    parser.add_argument("--end", type=_parse_date)
    parser.add_argument("--days", type=int)
    parser.add_argument("--symbols", nargs="+", metavar="SYMBOL")
    parser.add_argument("--markets", nargs="+", default=None, metavar="MARKET")
    parser.add_argument("--skip-tickers", action="store_true")
    parser.add_argument("--no-skip-done", action="store_true")
    args = parser.parse_args()

    # Pinned symbols path (if applicable) — skips master refresh
    if args.symbols:
        logger.info(
            f"=== <COLLECTOR>: {len(args.symbols)} pinned symbol(s) ==="
        )
        backfill_symbols(
            symbols=args.symbols,
            start_date=args.start, end_date=args.end, days=args.days,
            skip_done=not args.no_skip_done,
        )
        return

    if not args.skip_tickers:
        logger.info("=== Step 1: Refresh ticker master ===")
        collect_tickers_fdr(desc=True)
    else:
        logger.info("=== Step 1: Skipped ticker master refresh ===")

    logger.info("=== Step 2: Collect <data> ===")
    backfill_active_universe(
        start_date=args.start, end_date=args.end, days=args.days,
        markets=args.markets, skip_done=not args.no_skip_done,
    )


if __name__ == "__main__":
    main()
```

For date-iterating collectors, replace the `--symbols` branch with an
unconditional call to `backfill(...)`.

---

## 8. Smoke test (every collector module)

Every collector module ends with:

```python
if __name__ == "__main__":
    # Smoke test: small, real, fast
    backfill_symbols(["005930", "000660"], days=30)
    # OR for date-iterating:
    # backfill(days=7)
```

This is what users run to verify the module works in isolation.

---

## 9. Migration template

```sql
-- =========================================
-- 00X: <Description>
-- =========================================

-- 새 테이블 ----------------------------------------------------
CREATE TABLE IF NOT EXISTS <table> (
    symbol          VARCHAR(10)  NOT NULL,
    <pk_col>        <type>       NOT NULL,
    -- 데이터 컬럼들
    <col1>          NUMERIC(14,2),
    <col2>          BIGINT,
    -- ...
    -- 메타
    source          VARCHAR(20),               -- 어떤 collector가 채웠는지
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (symbol, <pk_col>)
);

-- 인덱스 -------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_<table>_<col>
    ON <table>(<col> DESC);

-- TimescaleDB hypertable (시계열인 경우만) -----------------------
SELECT create_hypertable(
    '<table>', '<time_col>',
    chunk_time_interval => INTERVAL '<1 month / 7 days / 1 year>',
    if_not_exists => TRUE
);
```

Chunk interval guideline:
- Daily-grain: `1 month`
- Minute-grain: `7 days`
- Quarterly/annual: `1 year`

---

## 10. Anti-patterns (recap, with examples)

```python
# ❌ DON'T: row-by-row INSERT
for _, row in df.iterrows():
    session.add(DailyPrice(**row.to_dict()))
# ✅ DO: bulk COPY + ON CONFLICT (see upsert_daily_prices)

# ❌ DON'T: requests
import requests
r = requests.get(url)
# ✅ DO: httpx (project dep)
import httpx
r = httpx.get(url, timeout=10)

# ❌ DON'T: skip retry wrapper
df = fdr.DataReader(symbol, start, end)
# ✅ DO: wrap in tenacity
df = _safe_fetch(symbol, start, end)

# ❌ DON'T: print
print(f"Got {len(df)} rows for {symbol}")
# ✅ DO: logger
logger.info(f"  {symbol}: {len(df)} rows")

# ❌ DON'T: catch and swallow
try:
    rows = collect_one(...)
except Exception:
    pass
# ✅ DO: log + record + continue (or break on circuit)
try:
    rows = collect_one(...)
except Exception as e:
    logger.error(f"  {sym}: {e}")
    log_collection(COLLECTOR_NAME, end_date, "failed",
                   symbol=sym, error_message=str(e)[:500])
```
