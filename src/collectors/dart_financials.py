"""DART major-account financial statements collector (Phase 2).

Source: fnlttSinglAcnt API (single-company major accounts)
  via OpenDartReader: dart.finstate(corp_code, bsns_year, reprt_code)

Iteration model:
  Triple-nested loop over (corp_code, bsns_year, reprt_code), with an
  inner pass for fs_div in (CFS, OFS) when configured for both.

  Total call count for a 2020-onwards backfill of ~2,500 listed companies:
    2,500 corps x 6 years x 4 quarters x 2 fs_div = 120,000 calls
  Personal-tier DART limit is 10,000/day, so this is a ~12-day backfill.
  Use --max-calls and re-run daily to spread the load.

Resume strategy:
  Before iterating, query dart_financials for corp_codes already present
  for the (year, reprt, fs_div) combo and skip those. This is more
  reliable than collection_log because data presence is the source of
  truth and survives log resets.

Failure handling:
  - DART responds with status='013' (no data) for many quarter/fs_div
    combos: we treat that as a successful empty fetch (not an error).
  - Status='020' means rate limit hit: we abort the whole run cleanly.
  - Other statuses: log and continue to the next (corp, year, reprt).

Account semantics reminder:
  - Income-statement items are CUMULATIVE (Q1=Q1, H1=Q1+Q2, Q3=Q1+Q2+Q3,
    FY=full year). Quarter-only values are derived at query time.
  - Balance-sheet items are POINT-IN-TIME at the report date.
  - DART returns 3 years per call (current/prior/prior-prior); we keep
    all three.
"""
from __future__ import annotations

import time
from datetime import date
from typing import Iterable

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from src.collectors.dart_common import get_dart_client
from src.config import get_app_config
from src.db.repositories import (
    get_completed_dart_financials_keys,
    get_listed_corp_codes,
    log_collection,
    upsert_dart_financials,
)
from src.utils.logger import logger

COLLECTOR_NAME = "dart_financials"

# Quarter codes used by DART
REPRT_CODES = {
    "11013": "Q1",
    "11012": "H1",
    "11014": "Q3",
    "11011": "FY",
}

# Default backfill window, per Phase 2 decision
DEFAULT_START_YEAR = 2020
DEFAULT_FS_DIVS = ("CFS", "OFS")

# DART API status codes we recognize
_STATUS_OK = "000"
_STATUS_NO_DATA = "013"  # "조회된 데이타가 없습니다."
_STATUS_RATE_LIMIT = "020"  # 사용한도초과
_STATUS_INVALID_KEY = "010"


def _iter_year_quarter_pairs(
    start_year: int,
    end_year: int,
    quarters: Iterable[str],
) -> list[tuple[int, str]]:
    """Yield (year, reprt_code) pairs in chronological order.

    Going chronologically lets a partial run leave a clean prefix \u2014
    if the process dies on 2024 Q3, you have everything through 2024 Q1.
    """
    out: list[tuple[int, str]] = []
    for y in range(start_year, end_year + 1):
        for q in quarters:
            out.append((y, q))
    return out


def _is_future_quarter(year: int, reprt_code: str, today: date) -> bool:
    """True if (year, reprt_code) hasn't been reported yet.

    Korean filing deadlines (post-period):
      Q1  ~ +45 days  (mid-May)
      H1  ~ +45 days  (mid-Aug)
      Q3  ~ +45 days  (mid-Nov)
      FY  ~ +90 days  (end of March, year+1)

    We use generous deadlines; DART will just return no-data for ones
    that haven't been filed yet, which we already handle gracefully.
    """
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
def _safe_finstate(dart, corp_code: str, year: int, reprt_code: str) -> pd.DataFrame:
    """Call dart.finstate with retry. Returns empty DataFrame on no-data.

    OpenDartReader's finstate() returns:
      - DataFrame with rows when DART status='000'
      - Empty DataFrame when DART status='013' (no data) \u2014 we treat as OK
      - Prints + raises for other error statuses (we let tenacity retry)
    """
    df = dart.finstate(corp_code, year, reprt_code=reprt_code)
    return df if df is not None else pd.DataFrame()


def _normalize(
    df: pd.DataFrame,
    corp_code: str,
    bsns_year: int,
    reprt_code: str,
    fs_div: str,
) -> pd.DataFrame:
    """Conform DART finstate response to dart_financials schema.

    DART columns we expect (subset; varies slightly by version):
      rcept_no, reprt_code, bsns_year, corp_code, sj_div, sj_nm,
      account_id, account_nm, thstrm_nm, thstrm_amount, frmtrm_nm,
      frmtrm_amount, bfefrmtrm_nm, bfefrmtrm_amount, ord, fs_div, fs_nm

    Transformations:
      - Numeric strings -> Decimal-ready numbers (commas, '-' sign)
      - Filter to the requested fs_div (DART returns both when called
        without filter \u2014 we slice client-side for safety)
      - Drop rows missing account_nm (PK component)
      - Force corp_code/bsns_year/reprt_code to our canonical values
    """
    if df.empty:
        return df

    df = df.copy()

    # If fs_div column exists, restrict to the requested one. Some DART
    # responses are already filtered, others return both \u2014 belt and braces.
    if "fs_div" in df.columns:
        df = df[df["fs_div"] == fs_div]
    if df.empty:
        return df

    # Numeric coercion. DART sends amounts as strings with commas
    # (e.g. "1,234,567,890"). Empty / "-" / "\uc870\ud68c\uc548\ub428" mean NULL.
    for col in ("thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount", "thstrm_add_amount"):
        if col in df.columns:
            df[col] = (
                df[col]
                .astype(str)
                .str.replace(",", "", regex=False)
                .str.strip()
            )
            df[col] = df[col].replace(
                {"": None, "-": None, "nan": None, "None": None,
                 "\uc870\ud68c\uc548\ub428": None, "NaN": None}
            )
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # ord can be a string in some responses
    if "ord" in df.columns:
        df["ord"] = pd.to_numeric(df["ord"], errors="coerce").astype("Int64")

    # account_nm: required, trim, cap to schema length
    if "account_nm" in df.columns:
        df["account_nm"] = df["account_nm"].astype(str).str.strip().str.slice(0, 200)
        df = df[df["account_nm"].notna() & (df["account_nm"] != "")]
    else:
        return pd.DataFrame()  # malformed response

    # account_id length cap; keep NULL if missing
    if "account_id" in df.columns:
        df["account_id"] = df["account_id"].astype(str).str.strip().str.slice(0, 50)
        df["account_id"] = df["account_id"].replace({"": None, "nan": None})
    else:
        df["account_id"] = None

    if "sj_div" in df.columns:
        df["sj_div"] = df["sj_div"].astype(str).str.strip().str.slice(0, 10)
    else:
        df["sj_div"] = None

    # Force canonical PK values (don't trust the response \u2014 zero-pad,
    # int-cast year, etc.)
    df["corp_code"] = str(corp_code).zfill(8)
    df["bsns_year"] = int(bsns_year)
    df["reprt_code"] = str(reprt_code)
    df["fs_div"] = fs_div

    # currency: DART responds with 'KRW' in 'currency' col; default if missing
    if "currency" in df.columns:
        df["currency"] = (
            df["currency"].astype(str).str.strip().str.slice(0, 10).replace({"": "KRW"})
        )
    else:
        df["currency"] = "KRW"

    df["source"] = "dart_fnltt_single_acnt"

    # Within (year, reprt, fs_div) we expect account_nm to be unique. If
    # DART has duplicates (rare \u2014 happens with corrected filings), keep
    # the last one (last-write-wins).
    df = df.drop_duplicates(
        subset=["corp_code", "bsns_year", "reprt_code", "fs_div", "account_nm"],
        keep="last",
    )

    keep = [
        "corp_code", "bsns_year", "reprt_code", "fs_div", "account_nm",
        "sj_div", "account_id",
        "thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount",
        "thstrm_add_amount",
        "currency", "ord", "source",
    ]
    keep = [c for c in keep if c in df.columns]
    return df[keep].copy()


def collect_financials(
    start_year: int = DEFAULT_START_YEAR,
    end_year: int | None = None,
    reprt_codes: Iterable[str] = tuple(REPRT_CODES.keys()),
    fs_divs: Iterable[str] = DEFAULT_FS_DIVS,
    corp_codes: list[str] | None = None,
    skip_done: bool = True,
    max_calls: int | None = None,
    request_delay: float | None = None,
) -> dict[str, int]:
    """Collect DART major-account financials.

    Args:
        start_year, end_year: inclusive year range. end_year defaults to
            current calendar year.
        reprt_codes: iterable of DART quarter codes. Default: all 4.
        fs_divs: iterable of 'CFS' / 'OFS'. Default: both.
        corp_codes: explicit corp_code list. None = all listed companies.
        skip_done: skip (corp, year, reprt, fs_div) combos already in DB.
        max_calls: hard cap on API calls this run (rate-limit safety).
            None = no limit.
        request_delay: seconds between API calls. Default from config.

    Returns counts: {ok, empty, failed, skipped, rows_inserted, calls}.
    """
    cfg = get_app_config()
    end_year = end_year or date.today().year
    if request_delay is None:
        request_delay = cfg.collection.daily.request_delay

    if corp_codes is None:
        pairs = get_listed_corp_codes()
        corp_codes = [c for c, _ in pairs]
    if not corp_codes:
        logger.warning("[dart-fin] No corp_codes to process. "
                       "Run dart_corp_codes collector first.")
        return {"ok": 0, "empty": 0, "failed": 0, "skipped": 0,
                "rows_inserted": 0, "calls": 0}

    today = date.today()
    yq_pairs = _iter_year_quarter_pairs(start_year, end_year, reprt_codes)

    # Drop future quarters that obviously haven't been reported
    yq_pairs = [(y, q) for y, q in yq_pairs if not _is_future_quarter(y, q, today)]

    logger.info(
        f"[dart-fin] {start_year}-{end_year}, "
        f"reprt={list(reprt_codes)}, fs_div={list(fs_divs)}, "
        f"corps={len(corp_codes):,}, year-quarter pairs={len(yq_pairs)}"
    )

    dart = get_dart_client()

    ok = 0
    empty = 0
    failed = 0
    skipped = 0
    rows_inserted = 0
    calls = 0

    # Outer loop: (year, reprt) so we can pre-fetch the resume set per combo
    for (year, reprt) in yq_pairs:
        for fs_div in fs_divs:
            if max_calls is not None and calls >= max_calls:
                logger.warning(
                    f"[dart-fin] Reached max_calls={max_calls}, stopping. "
                    f"Re-run later to continue."
                )
                return {"ok": ok, "empty": empty, "failed": failed,
                        "skipped": skipped, "rows_inserted": rows_inserted,
                        "calls": calls}

            done: set[str] = set()
            if skip_done:
                done = get_completed_dart_financials_keys(year, reprt, fs_div)

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
                try:
                    raw = _safe_finstate(dart, corp_code, year, reprt)
                    calls += 1
                    norm = _normalize(raw, corp_code, year, reprt, fs_div)

                    if norm.empty:
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
                    else:
                        n = upsert_dart_financials(norm)
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
                except Exception as e:
                    failed += 1
                    msg = str(e)[:400]
                    logger.error(f"  {corp_code} {year}/{reprt}/{fs_div}: {msg}")
                    log_collection(
                        COLLECTOR_NAME,
                        target_date=date(year, 12, 31),
                        symbol=corp_code,
                        status="failed",
                        duration_ms=int((time.time() - t0) * 1000),
                        error_message=f"{msg};{reprt};{fs_div}",
                    )
                    # Bail out on rate limit
                    if "020" in msg or "\ud55c\ub3c4\ucd08\uacfc" in msg or "rate" in msg.lower():
                        logger.error("[dart-fin] Daily API limit hit \u2014 aborting run")
                        return {"ok": ok, "empty": empty, "failed": failed,
                                "skipped": skipped, "rows_inserted": rows_inserted,
                                "calls": calls}

                time.sleep(request_delay)

    logger.success(
        f"[dart-fin] done \u2014 ok: {ok:,}, empty: {empty:,}, failed: {failed:,}, "
        f"skipped: {skipped:,}, rows: {rows_inserted:,}, calls: {calls:,}"
    )
    return {"ok": ok, "empty": empty, "failed": failed,
            "skipped": skipped, "rows_inserted": rows_inserted, "calls": calls}


if __name__ == "__main__":
    # Smoke test: latest quarter, CFS only, all corps, hard 50-call cap
    collect_financials(
        start_year=date.today().year,
        end_year=date.today().year,
        reprt_codes=("11013",),
        fs_divs=("CFS",),
        max_calls=50,
    )
