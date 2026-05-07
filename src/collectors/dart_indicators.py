"""DART major financial-indicators collector (Phase 2).

Source: fnlttSinglIndx API
  https://opendart.fss.or.kr/api/fnlttSinglIndx.json
  GET parameters:
    crtfc_key, corp_code, bsns_year, reprt_code, idx_cl_code

Response: ~25 rows per (corp, year, reprt, idx_cl_code) call (when there
is data). DART pre-computes the standard ratios so we don't have to.

Why httpx (not OpenDartReader):
  OpenDartReader does not expose fnlttSinglIndx as a typed method in
  current versions. Calling it via httpx directly is also more robust:
  we own the URL, the params, the timeout, and the error handling.

Iteration model:
  Quintuple loop over (corp_code, bsns_year, reprt_code, fs_div, idx_cl).
  fnlttSinglIndx requires `idx_cl_code` to filter by indicator class
  (M210000/M220000/M230000/M240000), so we always loop all four and
  concatenate.

  Wait — a clarifying note on fs_div:
    fnlttSinglIndx does NOT take fs_div as a request parameter. It
    returns rows that include both CFS and OFS (when both are filed).
    We split client-side and store separately.

Total call count for the 2020+ backfill of ~2,500 listed companies:
  2,500 x 6 years x 4 quarters x 4 idx_cl = 240,000 calls
That's 25-day backfill at the personal 10,000/day cap — heavier than
financials. Use --max-calls and re-run daily.

Resume strategy: same as dart_financials — skip (corp, year, reprt,
fs_div) combos already in dart_indicators.
"""
from __future__ import annotations

import time
from datetime import date
from typing import Iterable

import httpx
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from src.config import get_app_config
from src.db.repositories import (
    get_completed_dart_indicators_keys,
    get_listed_corp_codes,
    log_collection,
    upsert_dart_indicators,
)
from src.utils.logger import logger

COLLECTOR_NAME = "dart_indicators"

# Quarter codes used by DART (re-exported for the pipeline)
REPRT_CODES = {
    "11013": "Q1",
    "11012": "H1",
    "11014": "Q3",
    "11011": "FY",
}

# Indicator class codes that fnlttSinglIndx requires us to query
IDX_CL_CODES = {
    "M210000": "수익성지표",
    "M220000": "안정성지표",
    "M230000": "성장성지표",
    "M240000": "활동성지표",
}

DEFAULT_START_YEAR = 2020
DEFAULT_FS_DIVS = ("CFS", "OFS")

DART_INDX_URL = "https://opendart.fss.or.kr/api/fnlttSinglIndx.json"

_STATUS_OK = "000"
_STATUS_NO_DATA = "013"
_STATUS_RATE_LIMIT = "020"


def _is_future_quarter(year: int, reprt_code: str, today: date) -> bool:
    """Same heuristic as dart_financials._is_future_quarter."""
    quarter_end_month = {"11013": 3, "11012": 6, "11014": 9, "11011": 12}[reprt_code]
    grace_days = 90 if reprt_code == "11011" else 45
    period_end = date(year, quarter_end_month, 28)
    deadline = date.fromordinal(period_end.toordinal() + grace_days)
    return today < deadline


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=15),
    reraise=True,
)
def _safe_indx(
    client: httpx.Client,
    api_key: str,
    corp_code: str,
    year: int,
    reprt_code: str,
    idx_cl: str,
) -> tuple[str, list[dict]]:
    """Call fnlttSinglIndx. Returns (status, list_rows).

    Treats status='013' (no data) as a successful empty result, not an
    error. Raises for HTTP failures and explicit rate-limit (`020`).
    """
    params = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bsns_year": str(year),
        "reprt_code": reprt_code,
        "idx_cl_code": idx_cl,
    }
    resp = client.get(DART_INDX_URL, params=params, timeout=15.0)
    resp.raise_for_status()
    data = resp.json()
    status = str(data.get("status", ""))
    if status == _STATUS_OK:
        return status, list(data.get("list", []))
    if status == _STATUS_NO_DATA:
        return status, []
    if status == _STATUS_RATE_LIMIT:
        # Surface to caller so it can abort the run cleanly
        raise RuntimeError(f"DART rate limit (020): {data.get('message')}")
    # Any other code: treat as soft failure (log message, return empty)
    logger.warning(
        f"  DART status={status} for {corp_code}/{year}/{reprt_code}/{idx_cl}: "
        f"{data.get('message')}"
    )
    return status, []


def _normalize(
    rows: list[dict],
    corp_code: str,
    bsns_year: int,
    reprt_code: str,
    fs_div_filter: str,
) -> pd.DataFrame:
    """Conform DART indicator rows for a single fs_div to schema.

    Important: the API returns rows for BOTH fs_div='CFS' and fs_div='OFS'
    (when both are reported). The caller splits via `fs_div_filter` so
    we store one fs_div per upsert call.

    Numeric coercion:
      - thstrm_value etc. arrive as strings (sometimes with "%" suffix
        from older versions). Strip non-numeric chars before parsing.
      - DART signals "no value" via empty string or "-"
    """
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # Filter to the requested fs_div if the column is present
    if "fs_div" in df.columns:
        df = df[df["fs_div"] == fs_div_filter]
    if df.empty:
        return df

    df = df.copy()

    # Numeric coercion for the value columns
    for col in ("thstrm_value", "frmtrm_value", "bfefrmtrm_value"):
        if col in df.columns:
            df[col] = (
                df[col]
                .astype(str)
                .str.replace("%", "", regex=False)
                .str.replace(",", "", regex=False)
                .str.strip()
            )
            df[col] = df[col].replace(
                {"": None, "-": None, "nan": None, "None": None,
                 "조회안됨": None, "NaN": None}
            )
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # idx_nm is required (PK)
    if "idx_nm" in df.columns:
        df["idx_nm"] = df["idx_nm"].astype(str).str.strip().str.slice(0, 200)
        df = df[df["idx_nm"].notna() & (df["idx_nm"] != "")]
    else:
        return pd.DataFrame()

    # Other string fields with length caps
    for col, n in (("idx_cl_code", 7), ("idx_cl_nm", 50), ("idx_code", 20)):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip().str.slice(0, n)
            df[col] = df[col].replace({"": None, "nan": None})
        else:
            df[col] = None

    df["corp_code"] = str(corp_code).zfill(8)
    df["bsns_year"] = int(bsns_year)
    df["reprt_code"] = str(reprt_code)
    df["fs_div"] = fs_div_filter
    df["source"] = "dart_fnltt_single_indx"

    df = df.drop_duplicates(
        subset=["corp_code", "bsns_year", "reprt_code", "fs_div", "idx_nm"],
        keep="last",
    )

    keep = [
        "corp_code", "bsns_year", "reprt_code", "fs_div", "idx_nm",
        "idx_cl_code", "idx_cl_nm", "idx_code",
        "thstrm_value", "frmtrm_value", "bfefrmtrm_value",
        "source",
    ]
    keep = [c for c in keep if c in df.columns]
    return df[keep].copy()


def collect_indicators(
    start_year: int = DEFAULT_START_YEAR,
    end_year: int | None = None,
    reprt_codes: Iterable[str] = tuple(REPRT_CODES.keys()),
    fs_divs: Iterable[str] = DEFAULT_FS_DIVS,
    idx_cl_codes: Iterable[str] = tuple(IDX_CL_CODES.keys()),
    corp_codes: list[str] | None = None,
    skip_done: bool = True,
    max_calls: int | None = None,
    request_delay: float | None = None,
) -> dict[str, int]:
    """Collect DART major financial indicators.

    Args:
        start_year, end_year: inclusive year range.
        reprt_codes: quarter codes to collect.
        fs_divs: ('CFS',), ('OFS',), or both.
        idx_cl_codes: which indicator classes to pull (default: all 4).
        corp_codes: explicit list. None = all listed companies.
        skip_done: skip combos already present in dart_indicators.
        max_calls: hard cap on API calls this run.
        request_delay: seconds between API calls.
    """
    cfg = get_app_config()
    end_year = end_year or date.today().year
    if request_delay is None:
        request_delay = cfg.collection.daily.request_delay
    api_key = cfg.dart.api_key
    if not api_key:
        raise RuntimeError(
            "DART_API_KEY is not set. Get a key from https://opendart.fss.or.kr/ "
            "and add it to .env as DART_API_KEY=..."
        )

    if corp_codes is None:
        pairs = get_listed_corp_codes()
        corp_codes = [c for c, _ in pairs]
    if not corp_codes:
        logger.warning("[dart-idx] No corp_codes to process. "
                       "Run dart_corp_codes collector first.")
        return {"ok": 0, "empty": 0, "failed": 0, "skipped": 0,
                "rows_inserted": 0, "calls": 0}

    today = date.today()

    # Build (year, reprt) list, excluding obvious future quarters
    yq_pairs: list[tuple[int, str]] = []
    for y in range(start_year, end_year + 1):
        for q in reprt_codes:
            if not _is_future_quarter(y, q, today):
                yq_pairs.append((y, q))

    logger.info(
        f"[dart-idx] {start_year}-{end_year}, reprt={list(reprt_codes)}, "
        f"fs_div={list(fs_divs)}, idx_cl={list(idx_cl_codes)}, "
        f"corps={len(corp_codes):,}, year-quarter pairs={len(yq_pairs)}"
    )

    ok = 0
    empty = 0
    failed = 0
    skipped = 0
    rows_inserted = 0
    calls = 0

    with httpx.Client() as client:
        for (year, reprt) in yq_pairs:
            for fs_div in fs_divs:
                if max_calls is not None and calls >= max_calls:
                    break

                done: set[str] = set()
                if skip_done:
                    done = get_completed_dart_indicators_keys(year, reprt, fs_div)

                todo = [c for c in corp_codes if c not in done]
                if not todo:
                    logger.info(
                        f"  {year} {REPRT_CODES[reprt]} {fs_div}: "
                        f"all {len(corp_codes):,} corps already done"
                    )
                    skipped += len(corp_codes)
                    continue

                logger.info(
                    f"  {year} {REPRT_CODES[reprt]} {fs_div}: "
                    f"{len(todo):,} corps to fetch ({len(done):,} skipped)"
                )
                skipped += len(done)

                iter_desc = f"{year}{REPRT_CODES[reprt]}/{fs_div}"
                for corp_code in tqdm(todo, desc=iter_desc, leave=False):
                    if max_calls is not None and calls >= max_calls:
                        break

                    t0 = time.time()
                    all_rows: list[dict] = []
                    any_call_ok = False
                    try:
                        # Loop over indicator classes; each is a separate call
                        for idx_cl in idx_cl_codes:
                            if max_calls is not None and calls >= max_calls:
                                break
                            status, rows = _safe_indx(
                                client, api_key, corp_code, year, reprt, idx_cl
                            )
                            calls += 1
                            if status == _STATUS_OK:
                                any_call_ok = True
                                all_rows.extend(rows)
                            time.sleep(request_delay)

                        if not all_rows:
                            empty += 1
                            log_collection(
                                COLLECTOR_NAME,
                                target_date=date(year, 12, 31),
                                symbol=corp_code,
                                status="success",
                                rows_inserted=0,
                                duration_ms=int((time.time() - t0) * 1000),
                                error_message=f"empty;{reprt};{fs_div}",
                            )
                            continue

                        norm = _normalize(all_rows, corp_code, year, reprt, fs_div)
                        if norm.empty:
                            empty += 1
                            log_collection(
                                COLLECTOR_NAME,
                                target_date=date(year, 12, 31),
                                symbol=corp_code,
                                status="success",
                                rows_inserted=0,
                                duration_ms=int((time.time() - t0) * 1000),
                                error_message=f"empty_after_norm;{reprt};{fs_div}",
                            )
                        else:
                            n = upsert_dart_indicators(norm)
                            rows_inserted += n
                            ok += 1
                            log_collection(
                                COLLECTOR_NAME,
                                target_date=date(year, 12, 31),
                                symbol=corp_code,
                                status="success",
                                rows_inserted=n,
                                duration_ms=int((time.time() - t0) * 1000),
                                error_message=f"{reprt};{fs_div}",
                            )

                    except RuntimeError as e:
                        # Rate limit hit — abort cleanly
                        if "020" in str(e) or "rate" in str(e).lower():
                            logger.error(
                                "[dart-idx] Daily API limit hit — aborting run"
                            )
                            log_collection(
                                COLLECTOR_NAME,
                                target_date=date(year, 12, 31),
                                symbol=corp_code,
                                status="failed",
                                duration_ms=int((time.time() - t0) * 1000),
                                error_message=f"rate_limit;{reprt};{fs_div}",
                            )
                            return {"ok": ok, "empty": empty, "failed": failed,
                                    "skipped": skipped,
                                    "rows_inserted": rows_inserted,
                                    "calls": calls}
                        raise
                    except Exception as e:
                        failed += 1
                        msg = str(e)[:400]
                        logger.error(
                            f"  {corp_code} {year}/{reprt}/{fs_div}: {msg}"
                        )
                        log_collection(
                            COLLECTOR_NAME,
                            target_date=date(year, 12, 31),
                            symbol=corp_code,
                            status="failed",
                            duration_ms=int((time.time() - t0) * 1000),
                            error_message=f"{msg};{reprt};{fs_div}",
                        )

    logger.success(
        f"[dart-idx] done — ok: {ok:,}, empty: {empty:,}, failed: {failed:,}, "
        f"skipped: {skipped:,}, rows: {rows_inserted:,}, calls: {calls:,}"
    )
    return {"ok": ok, "empty": empty, "failed": failed,
            "skipped": skipped, "rows_inserted": rows_inserted, "calls": calls}


if __name__ == "__main__":
    # Smoke test: latest year, Q1, CFS only, hard 50-call cap
    collect_indicators(
        start_year=date.today().year,
        end_year=date.today().year,
        reprt_codes=("11013",),
        fs_divs=("CFS",),
        max_calls=50,
    )
