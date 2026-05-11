"""Smoke test for KIS account info queries.

Usage:
    python -m scripts.test_kis_account
    python -m scripts.test_kis_account --code 005930 --price 70000
    python -m scripts.test_kis_account --no-kor    # 한글 뷰 생략

What this verifies:
    1. .env has KIS_ACCOUNT_NO / KIS_ACCOUNT_PRODUCT populated.
    2. inquire_balance returns a (holdings_df, summary_df) pair without
       business errors. Output is rendered with both raw KIS field
       codes and Korean names side-by-side.
    3. inquire_psbl_order returns a 1-row DataFrame, also rendered with
       Korean names.

This script does not place orders. It exercises read-only endpoints
against whichever mode (paper/real) is configured in .env.
"""
from __future__ import annotations

# Load .env BEFORE importing modules that read os.environ at import time.
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import argparse  # noqa: E402

import pandas as pd  # noqa: E402

from src.config import get_kis_settings  # noqa: E402
from src.kis import (  # noqa: E402
    BALANCE_HOLDINGS_KOR,
    KISAccountError,
    get_kis_account,
)
from src.utils.logger import logger  # noqa: E402


def _check_account_settings() -> bool:
    settings = get_kis_settings()
    if not settings.kis_account_no or not settings.kis_account_product:
        logger.error(
            "KIS account info empty. Fill in .env:\n"
            "    KIS_ACCOUNT_NO=12345678        # 계좌번호 앞 8자리\n"
            "    KIS_ACCOUNT_PRODUCT=01         # 계좌번호 뒤 2자리"
        )
        return False
    logger.info(
        f"Mode: {settings.kis_mode}  |  account: "
        f"{settings.kis_account_no}-{settings.kis_account_product}"
    )
    return True


def _render_holdings(holdings: pd.DataFrame) -> None:
    """Print holdings with both raw and KOR column names side by side."""
    if holdings.empty:
        logger.info("(보유 종목 없음)")
        return

    # Rename to "raw / KOR" stacked headers to keep both visible.
    cols_of_interest = [
        "pdno",
        "prdt_name",
        "hldg_qty",
        "ord_psbl_qty",
        "pchs_avg_pric",
        "prpr",
        "evlu_amt",
        "evlu_pfls_amt",
        "evlu_pfls_rt",
    ]
    present = [c for c in cols_of_interest if c in holdings.columns]
    if not present:
        # Schema we don't recognize — fall back to printing everything.
        logger.info("\n" + holdings.to_string(index=False))
        return

    sub = holdings[present].copy()
    # Build "code\n한글명" double-line headers so users see both.
    sub.columns = [f"{c}\n{BALANCE_HOLDINGS_KOR.get(c, c)}" for c in present]
    logger.info("\n" + sub.to_string(index=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="KIS account info smoke test")
    parser.add_argument(
        "--code",
        default="005930",
        help="Stock code for inquire_psbl_order (default: 005930 / 삼성전자).",
    )
    parser.add_argument(
        "--price",
        type=int,
        default=0,
        help="Order price for inquire_psbl_order (0 = let KIS use current price).",
    )
    parser.add_argument(
        "--no-kor",
        action="store_true",
        help="Skip the Korean-name views (raw output only).",
    )
    args = parser.parse_args()

    if not _check_account_settings():
        return 1

    acct = get_kis_account()
    # Show full DataFrame contents (don't truncate Korean text in vertical views).
    pd.set_option("display.max_rows", None)
    pd.set_option("display.max_colwidth", None)
    pd.set_option("display.width", 160)

    # --- inquire_balance ----------------------------------------------
    try:
        logger.info("=== Step 1: inquire_balance (잔고조회) ===")
        holdings, summary = acct.inquire_balance()
    except KISAccountError as e:
        logger.error(f"inquire_balance failed: {e}")
        return 2
    except Exception as e:  # noqa: BLE001
        logger.exception(f"Unexpected error during inquire_balance: {e}")
        return 3

    # Holdings (output1)
    logger.success(f"holdings rows: {len(holdings)}")
    _render_holdings(holdings)

    # Summary (output2) — vertical KOR view
    logger.success(f"summary rows: {len(summary)}")
    if not summary.empty and not args.no_kor:
        logger.info("\n[잔고 요약 (원본코드 / 한글명 / 값)]")
        kor_summary = acct.summary_kor(summary)
        logger.info("\n" + kor_summary.to_string(index=False))
    elif not summary.empty:
        logger.info("\n" + summary.iloc[0].to_string())

    # --- inquire_psbl_order -------------------------------------------
    try:
        logger.info(
            f"=== Step 2: inquire_psbl_order (매수가능조회) "
            f"code={args.code} price={args.price} ==="
        )
        psbl = acct.inquire_psbl_order(stock_code=args.code, price=args.price)
    except KISAccountError as e:
        logger.error(f"inquire_psbl_order failed: {e}")
        return 4
    except Exception as e:  # noqa: BLE001
        logger.exception(f"Unexpected error during inquire_psbl_order: {e}")
        return 5

    logger.success(f"psbl_order rows: {len(psbl)}")
    if not psbl.empty and not args.no_kor:
        logger.info("\n[매수가능 (원본코드 / 한글명 / 값)]")
        kor_psbl = acct.summary_kor(psbl)
        logger.info("\n" + kor_psbl.to_string(index=False))
    elif not psbl.empty:
        logger.info("\n" + psbl.iloc[0].to_string())

    logger.success("KIS account smoke test PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
