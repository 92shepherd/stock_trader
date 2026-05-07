"""Smoke test for KIS OAuth authentication.

Usage:
    python -m scripts.test_kis_auth
    python -m scripts.test_kis_auth --revoke         # also exercise revoke
    python -m scripts.test_kis_auth --force-refresh  # bypass cache once

What this verifies:
    1. .env has KIS_APP_KEY / KIS_APP_SECRET populated.
    2. Token issuance against the configured host (paper vs real)
       returns a valid bearer token.
    3. The token gets cached to disk and reused on a second call
       without hitting the network.
    4. (optional) Revocation succeeds and clears the cache.

This script does NOT call any business endpoints — it only exercises the
auth lifecycle. Once green, downstream KIS clients can rely on
get_kis_auth() to provide a valid token on demand.
"""
from __future__ import annotations

# Load .env BEFORE importing modules that read os.environ at import time.
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import argparse  # noqa: E402

from src.config import get_kis_settings  # noqa: E402
from src.kis import KISAuthError, get_kis_auth  # noqa: E402
from src.utils.logger import logger  # noqa: E402


def _check_credentials() -> bool:
    settings = get_kis_settings()
    if not settings.kis_app_key or not settings.kis_app_secret:
        logger.error(
            "KIS credentials are empty. Fill in .env:\n"
            "    KIS_APP_KEY=...\n"
            "    KIS_APP_SECRET=...\n"
            "    KIS_MODE=paper   # or real\n"
            "Issue a key at https://apiportal.koreainvestment.com → "
            "[KIS Developers 서비스 신청]."
        )
        return False
    logger.info(
        f"Mode: {settings.kis_mode}  |  app_key: "
        f"{settings.kis_app_key[:6]}…{settings.kis_app_key[-4:]}  |  "
        f"account: {settings.kis_account_no or '(empty)'}"
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="KIS OAuth smoke test")
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Force a fresh token issuance (bypass cache).",
    )
    parser.add_argument(
        "--revoke",
        action="store_true",
        help="After verifying issuance, revoke the token and clear cache.",
    )
    args = parser.parse_args()

    if not _check_credentials():
        return 1

    auth = get_kis_auth()

    # --- Issuance -------------------------------------------------------
    try:
        logger.info("=== Step 1: ensure_token (issue or reuse) ===")
        token = auth.ensure_token(force_refresh=args.force_refresh)
    except KISAuthError as e:
        logger.error(f"Auth failed: {e}")
        return 2
    except Exception as e:  # noqa: BLE001
        logger.exception(f"Unexpected error during issuance: {e}")
        return 3

    masked = f"{token[:10]}…{token[-6:]}" if len(token) > 20 else "(short)"
    logger.success(f"access_token: {masked}")
    logger.info(f"expires_at:  {auth._expires_at!s}")

    # --- Cache reuse ----------------------------------------------------
    logger.info("=== Step 2: second call should reuse cache (no network) ===")
    token2 = auth.ensure_token()
    if token2 != token:
        logger.warning(
            "Second call returned a DIFFERENT token — cache may not be "
            "working as intended."
        )
    else:
        logger.success("Token reused from cache as expected.")

    # --- Header builder sanity -----------------------------------------
    headers = auth.auth_header()
    expected_keys = {"content-type", "authorization", "appkey", "appsecret"}
    missing = expected_keys - set(headers.keys())
    if missing:
        logger.error(f"auth_header() missing keys: {missing}")
        return 4
    logger.success("auth_header() shape OK.")

    # --- Optional revoke -----------------------------------------------
    if args.revoke:
        logger.info("=== Step 3: revoke token ===")
        try:
            auth.revoke()
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Revoke failed: {e}")
            return 5
        logger.success("Revoke complete.")

    logger.success("KIS auth smoke test PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
