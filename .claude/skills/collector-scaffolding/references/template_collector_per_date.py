"""<COLLECTOR_PURPOSE — one line>.

Strategy:
    <Explain WHY date-iterating is right for this upstream. Mention:
    - Upstream API shape (per-period, all-symbols)
    - Approximate cost per backfill (HTTP calls × periods, not symbols)
    - Why the expected request_delay
    - What does NOT come back from this source>

What this module does NOT provide:
    - <column1> — <reason>
    Combine with <other_collector> via ON CONFLICT DO UPDATE.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import <upstream_lib>  # TODO: replace
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from src.config import get_app_config
from src.db.repositories import (
    get_missing_dates,
    log_collection,
    upsert_<table>,  # TODO: replace
)
from src.utils.logger import logger

COLLECTOR_NAME = "<snake_case_name>"  # TODO: replace

# Expected columns from upstream. If NONE present, response is invalid
# (upstream outage / format change) and we skip the period.
_EXPECTED_COLS = {"<col1>", "<col2>"}  # TODO: adjust


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _safe_call(func, *args, **kwargs):
    """Wrap an upstream call with retry + backoff."""
    return func(*args, **kwargs)


def _rename_if_present(df: pd.DataFrame, mapping: dict[str, str]) -> pd.DataFrame:
    """Rename only columns that exist. Returns a copy with ONLY target cols."""
    existing = {k: v for k, v in mapping.items() if k in df.columns}
    df = df.rename(columns=existing)
    keep = list(existing.values())
    return df[keep].copy() if keep else pd.DataFrame()


def _fetch_one_period(target: date, market: str) -> pd.DataFrame:
    """Fetch all-symbol data for one market on one period.

    Returns empty DataFrame (not raises) when:
      - Upstream returned no data (holiday / transient API issue)
      - Response is malformed
    """
    # TODO: adapt to actual upstream API
    try:
        df = _safe_call(<upstream_lib>.<function>, target, market=market)
    except Exception as e:
        logger.warning(f"  {target} [{market}] fetch failed: {e}")
        return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    # Defensive: verify response shape
    if not (set(df.columns) & _EXPECTED_COLS):
        logger.warning(
            f"  {target} [{market}] response has unexpected columns: "
            f"{list(df.columns)} — treating as empty"
        )
        return pd.DataFrame()

    df = _rename_if_present(df, {
        "<UpstreamCol1>": "<table_col1>",
        # ...
    })

    if df.empty or "<key_col>" not in df.columns:
        return pd.DataFrame()

    # Promote index to symbol column if applicable
    df = df.reset_index().rename(columns={"<IndexName>": "symbol"})
    if "symbol" not in df.columns:
        logger.warning(
            f"  {target} [{market}] response missing symbol — skipping"
        )
        return pd.DataFrame()
    df["<time_col>"] = target

    return df


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
                logger.debug(f"  {target} [{market}]: {len(df)} rows")
            time.sleep(cfg.collection.<grain>.request_delay)  # TODO: adjust grain
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
    # Drop bad rows (e.g., null/zero key metric)
    # merged = merged[merged["<key>"].notna() & (merged["<key>"] > 0)]

    if merged.empty:
        return 0

    return upsert_<table>(merged)


def backfill(
    start_date: date | None = None,
    end_date: date | None = None,
    days: int | None = None,
    skip_done: bool = True,
) -> None:
    """Backfill <data> for a date range.

    Args:
        start_date: first period (inclusive). Defaults to end_date - days.
        end_date: last period (inclusive). Defaults to today.
        days: calendar days back from end_date. Defaults to config.
        skip_done: if True, skip periods already logged as success.
    """
    cfg = get_app_config()
    end_date = end_date or date.today()
    if start_date is None:
        days = days or cfg.collection.<grain>.backfill_days  # TODO: adjust grain
        start_date = end_date - timedelta(days=days)

    logger.info(f"Backfilling: {start_date} → {end_date}")

    if skip_done:
        targets = get_missing_dates(COLLECTOR_NAME, start_date, end_date)
        logger.info(
            f"Candidate periods (not yet successful): {len(targets)}"
        )
    else:
        targets = []
        d = start_date
        while d <= end_date:
            if d.weekday() < 5:  # Mon-Fri only
                targets.append(d)
            d += timedelta(days=1)
        logger.info(f"Candidate periods: {len(targets)}")

    if not targets:
        logger.success("Nothing to backfill — all target periods already done.")
        return

    total_rows = 0
    consecutive_failures = 0
    for target in tqdm(targets, desc="<descriptive label>"):  # TODO: adjust
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


if __name__ == "__main__":
    # Smoke test: last 7 days
    backfill(days=7)
