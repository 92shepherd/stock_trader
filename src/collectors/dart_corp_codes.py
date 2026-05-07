"""DART corp_code master collector.

Source: https://opendart.fss.or.kr/api/corpCode.xml
  → ZIP file containing CORPCODE.xml (~100k entries)

Why this exists:
  Every other DART API endpoint takes DART's own 8-digit `corp_code`,
  not our familiar 6-digit `stock_code`. So we cache the entire mapping
  locally and refresh it weekly (configurable). Without this table, the
  disclosure / financial-statement collectors can't do anything.

Implementation note:
  We rely on OpenDartReader for the heavy lifting — it downloads,
  unzips, and parses the XML into a pandas DataFrame in one call (via
  the `dart.corp_codes` attribute). Doing this manually means handling
  ZIP extraction, XML namespaces, and EUC-KR vs UTF-8 quirks; not worth
  the maintenance burden.

Freshness policy:
  By default, refresh only if the last update is older than
  `stale_after_days` (default 7). The `--force` flag in the pipeline
  overrides this for cases like new IPOs that aren't yet in the cache.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from src.collectors.dart_common import get_dart_client
from src.config import get_app_config
from src.db.repositories import (
    get_dart_corp_codes_freshness,
    log_collection,
    upsert_dart_corp_codes,
)
from src.utils.logger import logger

if TYPE_CHECKING:  # pragma: no cover
    from OpenDartReader import OpenDartReader  # noqa: F401

COLLECTOR_NAME = "dart_corp_codes"

# DART caps individual rows we expect; anything wildly different is an
# integrity red flag worth surfacing to the user.
_EXPECTED_MIN_ROWS = 50_000   # well under typical ~100k
_EXPECTED_MAX_ROWS = 300_000  # well over typical ~100k


def _get_dart_client():
    """Backwards-compat shim. New code should import
    `src.collectors.dart_common.get_dart_client` directly.
    """
    return get_dart_client()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    reraise=True,
)
def _fetch_corp_codes() -> pd.DataFrame:
    """Fetch the full corp_codes DataFrame from DART.

    OpenDartReader's constructor downloads & caches the ZIP; the
    `corp_codes` attribute exposes it as a DataFrame with columns:
      corp_code, corp_name, stock_code, modify_date

    `stock_code` is empty string '' for non-listed companies (we
    normalize to None below).
    """
    dart = _get_dart_client()
    df = dart.corp_codes
    if df is None or len(df) == 0:
        raise RuntimeError("OpenDartReader returned an empty corp_codes frame")
    return df.copy()


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Clean DART's raw corp_codes frame for our schema.

    Transformations:
      - stock_code: empty string → None (so the partial index works)
      - modify_date: 'YYYYMMDD' string → python date
      - corp_name: trim, cap to 200 chars (schema constraint)
    """
    df = df.copy()

    # stock_code: '' → NaN → None (na_rep='\\N' handles None correctly in CSV)
    if "stock_code" in df.columns:
        df["stock_code"] = (
            df["stock_code"]
            .astype(str)
            .str.strip()
            .replace({"": None, "nan": None, "None": None})
        )

    # modify_date: 'YYYYMMDD' → date
    if "modify_date" in df.columns:
        df["modify_date"] = pd.to_datetime(
            df["modify_date"], format="%Y%m%d", errors="coerce"
        ).dt.date

    # corp_name: trim, hard-cap to schema length
    if "corp_name" in df.columns:
        df["corp_name"] = (
            df["corp_name"].astype(str).str.strip().str.slice(0, 200)
        )

    # Drop rows missing the PK
    df = df[df["corp_code"].notna() & (df["corp_code"].astype(str).str.strip() != "")]
    df["corp_code"] = df["corp_code"].astype(str).str.zfill(8)

    return df[["corp_code", "corp_name", "stock_code", "modify_date"]]


def _is_fresh(stale_after_days: int) -> bool:
    """True iff dart_corp_codes is younger than stale_after_days."""
    last = get_dart_corp_codes_freshness()
    if last is None:
        return False
    # last is timezone-aware (TIMESTAMPTZ); compare in UTC
    age = datetime.now(timezone.utc) - last
    return age < timedelta(days=stale_after_days)


def collect_corp_codes(
    force: bool = False,
    stale_after_days: int | None = None,
) -> dict[str, int]:
    """Refresh the dart_corp_codes table.

    Args:
        force: bypass the freshness check and always re-download.
        stale_after_days: how old the existing data may be before we
            re-download. Default comes from config (cfg.dart.corp_codes_stale_after_days).

    Returns counts: {fetched, upserted, listed, non_listed}.
    """
    cfg = get_app_config()
    if stale_after_days is None:
        stale_after_days = cfg.dart.corp_codes_stale_after_days

    target_date = date.today()

    if not force and _is_fresh(stale_after_days):
        logger.info(
            f"[dart-corp] dart_corp_codes is fresh (within {stale_after_days} days). "
            "Skipping. Use --force to override."
        )
        log_collection(
            COLLECTOR_NAME, target_date, "skipped",
            error_message=f"fresh within {stale_after_days}d",
        )
        return {"fetched": 0, "upserted": 0, "listed": 0, "non_listed": 0}

    t0 = time.time()
    try:
        logger.info("[dart-corp] Fetching corp_codes from DART (via OpenDartReader)...")
        raw = _fetch_corp_codes()
        logger.info(f"  raw rows: {len(raw):,}")

        df = _normalize(raw)
        listed = int(df["stock_code"].notna().sum())
        non_listed = len(df) - listed
        logger.info(
            f"  normalized: {len(df):,} (listed={listed:,}, non_listed={non_listed:,})"
        )

        if not (_EXPECTED_MIN_ROWS <= len(df) <= _EXPECTED_MAX_ROWS):
            logger.warning(
                f"  row count {len(df):,} is outside expected range "
                f"[{_EXPECTED_MIN_ROWS:,}, {_EXPECTED_MAX_ROWS:,}] — "
                "DART may have changed format"
            )

        affected = upsert_dart_corp_codes(df)
        dur = int((time.time() - t0) * 1000)

        log_collection(
            COLLECTOR_NAME, target_date, "success",
            rows_inserted=affected,
            duration_ms=dur,
        )
        logger.success(
            f"[dart-corp] upserted {affected:,} rows "
            f"({listed:,} listed) in {dur}ms"
        )
        return {
            "fetched": len(raw),
            "upserted": affected,
            "listed": listed,
            "non_listed": non_listed,
        }
    except Exception as e:
        dur = int((time.time() - t0) * 1000)
        log_collection(
            COLLECTOR_NAME, target_date, "failed",
            error_message=str(e)[:500],
            duration_ms=dur,
        )
        logger.error(f"[dart-corp] failed: {e}")
        raise


if __name__ == "__main__":
    collect_corp_codes(force=True)
