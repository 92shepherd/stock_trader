"""US ticker master collector.

Source: NASDAQ Trader's official daily-updated ticker files served over
HTTPS (the legacy FTP is also still available).
  - https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt
    (NASDAQ + NYSE + AMEX + others, including ETFs, since 2010)
  - https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt
    (NYSE / AMEX / NYSEARCA / BATS — kept for redundancy)

Both files are pipe-delimited with a trailing "File Creation Time" line
that we drop. They are the canonical free source for US-listed
securities; yfinance does not expose a similar listing endpoint.

What we capture:
  - symbol, name, exchange, security_type (COMMON/ETF/etc.), is_etf flag
  - test_issue flag (NASDAQ test entries — exclude from collection)

What we DON'T capture here:
  - listing_date  : not in the files; can be backfilled later via
    yfinance fast_info on a per-symbol basis
  - sector / industry : not in the files; would need a separate source

Delisting handling:
  Symbols present in DB but missing from today's fresh download are
  marked delisted=TRUE, delisted_date=ref_date. Same pattern as the
  Korean `tickers.py` collector.
"""
from __future__ import annotations

from datetime import date
from io import StringIO

import httpx
import pandas as pd
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from tenacity import retry, stop_after_attempt, wait_exponential

from src.db.connection import session_scope
from src.db.models import TickerUS
from src.utils.logger import logger

COLLECTOR_NAME = "tickers_us"

# PostgreSQL 단일 쿼리 파라미터 한도는 65,535개. TickerUS 한 row당 8개
# 컬럼이므로 65535/8 ≈ 8,191 row가 이론적 상한이지만, 안전 마진을 두고
# 1,000 row씩 청크로 INSERT한다. 12,000+ 종목을 한 번에 보내면
# psycopg가 "too many parameters" 또는 쿼리 텍스트 길이 초과로 실패함.
_INSERT_CHUNK_SIZE = 1000
# UPDATE ... WHERE symbol IN (...) 절도 같은 한도에 영향을 받음.
# delisted 표시 대상이 많을 경우 대비 청크화.
_UPDATE_CHUNK_SIZE = 1000

NASDAQ_TRADED_URL = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt"
)
OTHER_LISTED_URL = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
)

# Mapping from NASDAQ Trader's single-letter exchange codes (used in
# nasdaqtraded.txt's "Listing Exchange" column) to our canonical names.
# Reference: https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs
_EXCHANGE_CODE_MAP = {
    "Q": "NASDAQ",   # NASDAQ Global Select
    "G": "NASDAQ",   # NASDAQ Global Market
    "S": "NASDAQ",   # NASDAQ Capital Market
    "N": "NYSE",
    "A": "AMEX",     # NYSE American (formerly AMEX)
    "P": "NYSEARCA", # NYSE Arca
    "Z": "BATS",     # Cboe BZX
    "V": "IEX",
}


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _fetch_url(url: str) -> str:
    """Fetch a NASDAQ Trader symbol file with retry on transient errors."""
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.text


def _parse_nasdaq_traded(text: str) -> pd.DataFrame:
    """Parse nasdaqtraded.txt.

    Format (pipe-delimited, header in first line):
      Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|
      ETF|Round Lot Size|Test Issue|Financial Status|CQS Symbol|
      NASDAQ Symbol|NextShares

    The last line is "File Creation Time: MMDDYYYYHHMM" — drop it.
    """
    lines = text.strip().splitlines()
    # Drop trailing "File Creation Time" footer
    if lines and lines[-1].startswith("File Creation Time"):
        lines = lines[:-1]
    body = "\n".join(lines)

    df = pd.read_csv(StringIO(body), sep="|", dtype=str, keep_default_na=False)

    # Filter: only "Nasdaq Traded" = "Y" rows (the NASDAQ Trader feed
    # sometimes includes deactivated rows flagged with "N" — we want
    # currently-traded names).
    if "Nasdaq Traded" in df.columns:
        df = df[df["Nasdaq Traded"] == "Y"].copy()

    rename = {
        "Symbol": "symbol",
        "Security Name": "name",
        "Listing Exchange": "_exchange_code",
        "ETF": "_etf_flag",
        "Test Issue": "_test_flag",
    }
    df = df.rename(columns=rename)

    df["exchange"] = df["_exchange_code"].map(_EXCHANGE_CODE_MAP).fillna("OTHER")
    df["is_etf"] = df["_etf_flag"].eq("Y")
    df["test_issue"] = df["_test_flag"].eq("Y")

    return df[["symbol", "name", "exchange", "is_etf", "test_issue"]]


def _parse_other_listed(text: str) -> pd.DataFrame:
    """Parse otherlisted.txt (NYSE / AMEX / NYSEARCA / BATS).

    Format (pipe-delimited, header in first line):
      ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|
      Test Issue|NASDAQ Symbol
    """
    lines = text.strip().splitlines()
    if lines and lines[-1].startswith("File Creation Time"):
        lines = lines[:-1]
    body = "\n".join(lines)

    df = pd.read_csv(StringIO(body), sep="|", dtype=str, keep_default_na=False)

    rename = {
        "ACT Symbol": "symbol",
        "Security Name": "name",
        "Exchange": "_exchange_code",
        "ETF": "_etf_flag",
        "Test Issue": "_test_flag",
    }
    df = df.rename(columns=rename)

    df["exchange"] = df["_exchange_code"].map(_EXCHANGE_CODE_MAP).fillna("OTHER")
    df["is_etf"] = df["_etf_flag"].eq("Y")
    df["test_issue"] = df["_test_flag"].eq("Y")

    return df[["symbol", "name", "exchange", "is_etf", "test_issue"]]


def _classify_security_type(name: str, is_etf: bool) -> str:
    """Heuristic classifier from the security name string.

    NASDAQ Trader files don't carry a clean security_type field — we
    have to infer from name suffixes. This is good enough for filtering;
    edge cases (units, rights) get bucketed as OTHER.

    Examples:
      "Apple Inc. - Common Stock"          -> COMMON
      "SPDR S&P 500 ETF Trust"             -> ETF
      "BlackRock Inc Series A Pfd Stk"     -> PREFERRED
      "Petrobras ADR"                      -> ADR
      "AcmeCo Warrant"                     -> WARRANT
    """
    if is_etf:
        return "ETF"
    if not isinstance(name, str):
        return "OTHER"
    n = name.upper()
    # Order matters - check more specific suffixes before "COMMON"
    if "WARRANT" in n or n.endswith(" WT") or " WTS" in n:
        return "WARRANT"
    if " UNIT" in n or n.endswith("UNITS"):
        return "UNIT"
    if "PREFERRED" in n or " PFD" in n or "PREFERENCE" in n:
        return "PREFERRED"
    if "ADR" in n or "ADS" in n:
        return "ADR"
    if "RIGHTS" in n or n.endswith(" RT"):
        return "RIGHTS"
    if "COMMON STOCK" in n or "ORDINARY SHARES" in n:
        return "COMMON"
    # Default: assume common stock if nothing else matched
    return "COMMON"


def _normalize_symbol(sym: str) -> str:
    """Map NASDAQ Trader's exotic separators to yfinance-compatible form.

    NASDAQ Trader files use:
      - BRK.B     for Berkshire class B common shares
      - ABR$F     for Arbor Realty series F preferred shares

    yfinance expects:
      - BRK-B     for class shares
      - ABR-PF    for preferred shares (the -P prefix marks preferred,
                  next char(s) = series letter)

    Rules applied here:
      1. $<X>   -> -P<X>   (preferred share series indicator)
      2. .      -> -       (class share separator)

    Note: yfinance's exact preferred-share symbology is not 100%
    documented and varies by series; some -P* symbols still 404.
    Those are caught downstream as empty results, not crashes.
    """
    if not isinstance(sym, str):
        return sym
    # Order matters: handle $ first since the result also contains -
    if "$" in sym:
        sym = sym.replace("$", "-P")
    sym = sym.replace(".", "-")
    return sym


def collect_us_tickers(ref_date: date | None = None) -> dict[str, int]:
    """Fetch & merge NASDAQ Trader's two listing files; upsert into tickers_us.

    Returns counts: {inserted, updated, delisted_marked, total_fresh}.
    """
    ref_date = ref_date or date.today()

    logger.info("[us-tickers] Fetching nasdaqtraded.txt ...")
    nasdaq_text = _fetch_url(NASDAQ_TRADED_URL)
    df_nasdaq = _parse_nasdaq_traded(nasdaq_text)
    logger.info(f"  nasdaqtraded.txt: {len(df_nasdaq)} rows")

    logger.info("[us-tickers] Fetching otherlisted.txt ...")
    other_text = _fetch_url(OTHER_LISTED_URL)
    df_other = _parse_other_listed(other_text)
    logger.info(f"  otherlisted.txt: {len(df_other)} rows")

    # nasdaqtraded.txt is the superset since 2010 - but otherlisted.txt
    # occasionally has rows missing from it (esp. for some BATS/IEX names).
    # Concat & dedupe by symbol; nasdaqtraded.txt wins on conflict.
    merged = pd.concat([df_nasdaq, df_other], ignore_index=True)
    before = len(merged)
    merged = merged.drop_duplicates(subset=["symbol"], keep="first")
    logger.info(f"  merged: {len(merged)} unique symbols (deduped {before - len(merged)})")

    # Drop rows with empty/missing symbol or name (rare but happens on bad lines)
    merged = merged[
        merged["symbol"].notna() & (merged["symbol"].str.strip() != "")
        & merged["name"].notna() & (merged["name"].str.strip() != "")
    ].copy()

    # Normalize symbols to yfinance form (BRK.B -> BRK-B, ABR$F -> ABR-PF)
    merged["symbol"] = merged["symbol"].astype(str).map(_normalize_symbol)

    # Infer security_type
    merged["security_type"] = merged.apply(
        lambda r: _classify_security_type(r["name"], r["is_etf"]),
        axis=1,
    )

    logger.info(f"[us-tickers] Total fresh tickers: {len(merged)}")
    logger.info(
        "  by security_type: "
        + ", ".join(
            f"{k}={v}" for k, v in merged["security_type"].value_counts().items()
        )
    )

    # Build payload
    payload = []
    for row in merged.itertuples(index=False):
        payload.append({
            "symbol": row.symbol,
            "name": row.name[:200],  # safety: respect VARCHAR(200)
            "exchange": row.exchange,
            "security_type": row.security_type,
            "is_etf": bool(row.is_etf),
            "test_issue": bool(row.test_issue),
            "delisted": False,
            "delisted_date": None,
        })

    fresh_symbols = {p["symbol"] for p in payload}

    with session_scope() as session:
        existing_symbols = {
            s for (s,) in session.execute(select(TickerUS.symbol)).all()
        }

        # ------------------------------------------------------------------
        # Chunked INSERT ... ON CONFLICT DO UPDATE
        # ------------------------------------------------------------------
        # PostgreSQL 의 단일 쿼리 파라미터 한도(65,535) 때문에 12,000+ row를
        # 한 번에 보낼 수 없다. _INSERT_CHUNK_SIZE 단위로 끊어 보낸다.
        if payload:
            total = len(payload)
            for i in range(0, total, _INSERT_CHUNK_SIZE):
                chunk = payload[i:i + _INSERT_CHUNK_SIZE]
                stmt = pg_insert(TickerUS).values(chunk)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["symbol"],
                    set_={
                        "name": stmt.excluded.name,
                        "exchange": stmt.excluded.exchange,
                        "security_type": stmt.excluded.security_type,
                        "is_etf": stmt.excluded.is_etf,
                        "test_issue": stmt.excluded.test_issue,
                        "delisted": False,
                        "delisted_date": None,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
                session.execute(stmt)
                logger.debug(
                    f"  upserted chunk {i // _INSERT_CHUNK_SIZE + 1}/"
                    f"{(total + _INSERT_CHUNK_SIZE - 1) // _INSERT_CHUNK_SIZE} "
                    f"({len(chunk)} rows)"
                )

        inserted = len(fresh_symbols - existing_symbols)
        updated = len(fresh_symbols & existing_symbols)

        # ------------------------------------------------------------------
        # Chunked UPDATE for delisted marking
        # ------------------------------------------------------------------
        # IN (...) 절도 파라미터 한도의 영향을 받으므로 분할 처리.
        missing = existing_symbols - fresh_symbols
        delisted_marked = 0
        if missing:
            missing_list = list(missing)
            for i in range(0, len(missing_list), _UPDATE_CHUNK_SIZE):
                chunk_syms = missing_list[i:i + _UPDATE_CHUNK_SIZE]
                session.execute(
                    update(TickerUS)
                    .where(
                        TickerUS.symbol.in_(chunk_syms),
                        TickerUS.delisted.is_(False),
                    )
                    .values(delisted=True, delisted_date=ref_date)
                )
            delisted_marked = len(missing)

    logger.success(
        f"[us-tickers] inserted: {inserted}, updated: {updated}, "
        f"newly delisted: {delisted_marked}"
    )
    return {
        "inserted": inserted,
        "updated": updated,
        "delisted_marked": delisted_marked,
        "total_fresh": len(fresh_symbols),
    }


if __name__ == "__main__":
    collect_us_tickers()
