"""DART disclosures collector (Phase 1).

Source: https://opendart.fss.or.kr/api/list.json (via OpenDartReader)

Collection policy (decided in Phase 1 design):
  - LISTED COMPANIES ONLY     → drop rows where stock_code is empty
  - MAJOR-EVENT FOCUS (B)     → kind='B' is the priority signal but we
                                 store ALL kinds to keep the option open

Iteration model:
  Date-iterating, like daily_pykrx. For each calendar day in [start, end]:
    1. Call dart.list(start=d, end=d, kind=K) — returns ALL companies'
       disclosures of that kind on that day (no per-symbol loop needed!)
    2. Filter to listed-only
    3. Bulk upsert keyed by rcept_no

  This is dramatically more efficient than per-symbol iteration: a typical
  trading day has ~200-500 disclosures across ~2,500 listed names, and
  we get them in a single API call (or a few, if pagination kicks in).

Pagination:
  OpenDartReader's `list()` handles pagination internally and returns
  all results as one DataFrame, so we don't have to.

Resume:
  collection_log keyed by (collector='dart_disclosures', target_date=d).
  symbol is NULL because we collect a date's worth of disclosures across
  all symbols at once, not per-symbol.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text as sql_text
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from src.collectors.dart_common import get_dart_client
from src.config import get_app_config
from src.db.connection import session_scope
from src.db.repositories import (
    log_collection,
    upsert_dart_disclosures,
)
from src.utils.logger import logger

COLLECTOR_NAME = "dart_disclosures"

# Phase 1 priority kinds. We collect 'B' by default (major events);
# pipeline can override via --kinds.
DEFAULT_KINDS = ("B",)

# All valid DART kind codes (for validation / "all" expansion)
ALL_KINDS = ("A", "B", "C", "D", "E", "F", "G", "H", "I", "J")


def _get_dart_client():
    """Backwards-compat shim. New code should import
    `src.collectors.dart_common.get_dart_client` directly.
    """
    return get_dart_client()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    reraise=True,
)
def _safe_list(dart, target_date: date, kind: str) -> pd.DataFrame:
    """Fetch one (date, kind) bundle from DART with retry.

    OpenDartReader returns an empty DataFrame on no-data days
    (weekends, holidays); we treat that as a successful zero-row fetch.
    """
    df = dart.list(
        start=target_date.strftime("%Y-%m-%d"),
        end=target_date.strftime("%Y-%m-%d"),
        kind=kind,
        final=True,  # final=True = include amendments as their own rcept_no
    )
    return df if df is not None else pd.DataFrame()


def _normalize(df: pd.DataFrame, kind: str) -> pd.DataFrame:
    """Conform DART's list response to our dart_disclosures schema.

    DART columns we get:
      corp_cls, corp_name, corp_code, stock_code, report_nm, rcept_no,
      flr_nm, rcept_dt, rm

    Transformations:
      - stock_code: '' / whitespace → None (so listed-only filter works)
      - rcept_dt: 'YYYYMMDD' → python date
      - corp_name / report_nm / flr_nm: trim and cap to schema lengths
      - kind: from the request parameter (DART doesn't echo it)
      - kind_detail: leave None for now (Phase 1 doesn't parse subcodes)
    """
    if df.empty:
        return df

    df = df.copy()

    # Some OpenDartReader versions return slightly different column names
    # (e.g. 'flr_nm' vs 'submitter') — normalize the few we know about.
    rename = {}
    if "submitter" in df.columns and "flr_nm" not in df.columns:
        rename["submitter"] = "flr_nm"
    if rename:
        df = df.rename(columns=rename)

    # stock_code: empty/whitespace → None
    if "stock_code" in df.columns:
        df["stock_code"] = (
            df["stock_code"]
            .astype(str)
            .str.strip()
            .replace({"": None, "nan": None, "None": None})
        )
    else:
        df["stock_code"] = None

    # rcept_dt parsing — DART returns 'YYYYMMDD' as string
    df["rcept_dt"] = pd.to_datetime(
        df["rcept_dt"].astype(str), format="%Y%m%d", errors="coerce"
    ).dt.date

    # String length caps to satisfy schema
    if "corp_name" in df.columns:
        df["corp_name"] = df["corp_name"].astype(str).str.strip().str.slice(0, 200)
    if "report_nm" in df.columns:
        df["report_nm"] = df["report_nm"].astype(str).str.strip().str.slice(0, 500)
    if "flr_nm" in df.columns:
        df["flr_nm"] = df["flr_nm"].astype(str).str.strip().str.slice(0, 200)
    if "rm" in df.columns:
        df["rm"] = df["rm"].astype(str).str.strip().str.slice(0, 50)
        df["rm"] = df["rm"].replace({"": None, "nan": None})

    # corp_code is sometimes returned without leading zeros
    if "corp_code" in df.columns:
        df["corp_code"] = df["corp_code"].astype(str).str.zfill(8)

    # corp_cls: single char (Y/K/N/E)
    if "corp_cls" in df.columns:
        df["corp_cls"] = df["corp_cls"].astype(str).str.strip().str.slice(0, 1)

    df["kind"] = kind
    df["kind_detail"] = None

    # Required column: rcept_no must exist and be non-empty
    df = df[df["rcept_no"].notna() & (df["rcept_no"].astype(str).str.strip() != "")]
    df["rcept_no"] = df["rcept_no"].astype(str).str.strip()

    keep = [
        "rcept_no", "corp_code", "corp_name", "stock_code", "corp_cls",
        "report_nm", "rcept_dt", "flr_nm", "rm", "kind", "kind_detail",
    ]
    keep = [c for c in keep if c in df.columns]
    return df[keep].copy()


def _filter_listed(df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows whose stock_code is missing — Phase 1 listed-only policy."""
    if df.empty:
        return df
    return df[df["stock_code"].notna()].copy()


def _get_completed_dates(
    start_date: date, end_date: date, kinds: tuple[str, ...]
) -> set[date]:
    """Return dates that have a 'success' log entry for ALL requested kinds.

    Resume key is (collector='dart_disclosures', target_date=d, error_message
    contains 'kinds=...'). To stay simple, we mark the date complete only
    when all requested kinds for that date succeeded — we encode the
    kind set in the error_message field (using it as a metadata channel).
    """
    # Simple approach: a date is considered "done" if there's at least one
    # success row for it AND that row's metadata covers all requested kinds.
    kinds_marker = f"kinds={'+'.join(sorted(kinds))}"
    with session_scope() as session:
        rows = session.execute(
            sql_text("""
                SELECT target_date FROM collection_log
                 WHERE collector = :col
                   AND status = 'success'
                   AND target_date BETWEEN :s AND :e
                   AND COALESCE(error_message, '') LIKE :marker
            """),
            {
                "col": COLLECTOR_NAME,
                "s": start_date,
                "e": end_date,
                "marker": f"%{kinds_marker}%",
            },
        ).all()
    return {r[0] for r in rows}


def collect_disclosures(
    start_date: date | None = None,
    end_date: date | None = None,
    days: int | None = None,
    kinds: tuple[str, ...] | list[str] = DEFAULT_KINDS,
    listed_only: bool = True,
    skip_done: bool = True,
    request_delay: float | None = None,
) -> dict[str, int]:
    """Collect DART disclosures for a [start_date, end_date] range.

    Args:
        start_date / end_date / days: same semantics as other collectors.
            Default range is [today - days, today]; days defaults to 7.
        kinds: tuple of single-char DART kind codes (e.g. ('B',) for
            major events only, ('A','B','C') for the three most actionable).
            Pass ALL_KINDS to grab everything.
        listed_only: drop non-listed disclosures (default True per Phase 1).
        skip_done: resume — skip dates already fully processed.
        request_delay: seconds to sleep between API calls. Default
            from config (collection.daily.request_delay).

    Returns counts: {ok_dates, failed_dates, empty_dates, total_rows}.
    """
    cfg = get_app_config()
    end_date = end_date or date.today()
    if start_date is None:
        days = days if days is not None else 7  # Phase 1 default: 1 week
        start_date = end_date - timedelta(days=days)

    if isinstance(kinds, list):
        kinds = tuple(kinds)
    invalid = [k for k in kinds if k not in ALL_KINDS]
    if invalid:
        raise ValueError(
            f"Invalid DART kind code(s): {invalid}. Valid: {ALL_KINDS}"
        )

    if request_delay is None:
        request_delay = cfg.collection.daily.request_delay

    # Build the date list (calendar days — DART has weekend/holiday data
    # like edits/late filings, so we don't pre-skip weekends here).
    all_dates: list[date] = []
    d = start_date
    while d <= end_date:
        all_dates.append(d)
        d += timedelta(days=1)

    skipped_done = 0
    target_dates = all_dates
    if skip_done:
        done = _get_completed_dates(start_date, end_date, kinds)
        if done:
            before = len(target_dates)
            target_dates = [x for x in target_dates if x not in done]
            skipped_done = before - len(target_dates)
            logger.info(
                f"Resume: {skipped_done} date(s) already done for "
                f"kinds={'+'.join(sorted(kinds))}, {len(target_dates)} remaining"
            )

    if not target_dates:
        logger.success(
            "[dart-disc] Nothing to collect — every date already done."
        )
        return {
            "ok_dates": 0, "failed_dates": 0, "empty_dates": 0,
            "skipped_dates": skipped_done, "total_rows": 0,
        }

    logger.info(
        f"[dart-disc] {start_date} -> {end_date}, kinds={list(kinds)}, "
        f"listed_only={listed_only}, {len(target_dates)} date(s) to fetch"
    )

    dart = _get_dart_client()

    ok = 0
    failed = 0
    empty = 0
    total_rows = 0
    kinds_marker = f"kinds={'+'.join(sorted(kinds))}"

    for d in tqdm(target_dates, desc="dart disclosures"):
        t0 = time.time()
        try:
            day_total = 0
            day_frames: list[pd.DataFrame] = []
            for k in kinds:
                raw = _safe_list(dart, d, k)
                norm = _normalize(raw, k)
                if listed_only:
                    norm = _filter_listed(norm)
                if not norm.empty:
                    day_frames.append(norm)
                # Brief pause between kind-calls to be a polite client
                time.sleep(request_delay)

            if day_frames:
                merged = pd.concat(day_frames, ignore_index=True)
                # Within a date, dedupe by rcept_no (a disclosure could in
                # theory show up under multiple kinds, though rare)
                merged = merged.drop_duplicates(subset=["rcept_no"], keep="first")
                day_total = upsert_dart_disclosures(merged)

            dur = int((time.time() - t0) * 1000)
            if day_total > 0:
                ok += 1
                total_rows += day_total
                log_collection(
                    COLLECTOR_NAME, d, "success",
                    rows_inserted=day_total, duration_ms=dur,
                    error_message=kinds_marker,  # used as resume metadata
                )
            else:
                empty += 1
                # Still log as success — empty days (weekends, no events)
                # are legitimate completed states for resume purposes
                log_collection(
                    COLLECTOR_NAME, d, "success",
                    rows_inserted=0, duration_ms=dur,
                    error_message=f"empty;{kinds_marker}",
                )
        except Exception as e:
            dur = int((time.time() - t0) * 1000)
            failed += 1
            logger.error(f"  {d}: {e}")
            log_collection(
                COLLECTOR_NAME, d, "failed",
                error_message=f"{str(e)[:400]};{kinds_marker}",
                duration_ms=dur,
            )

    logger.success(
        f"[dart-disc] done — ok: {ok}, empty: {empty}, failed: {failed}, "
        f"skipped(done): {skipped_done}, total rows: {total_rows:,}"
    )
    return {
        "ok_dates": ok,
        "failed_dates": failed,
        "empty_dates": empty,
        "skipped_dates": skipped_done,
        "total_rows": total_rows,
    }


if __name__ == "__main__":
    # Smoke test: yesterday's major-event disclosures
    yesterday = date.today() - timedelta(days=1)
    collect_disclosures(start_date=yesterday, end_date=yesterday, kinds=("B",))
