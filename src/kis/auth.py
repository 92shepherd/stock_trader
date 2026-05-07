"""KIS Open API OAuth2 access-token management.

Strategy:
    KIS issues an access_token via POST /oauth2/tokenP using the
    client_credentials grant. The token is valid for 24 hours. KIS
    enforces a hard rate limit of "1 token issuance per minute"
    (https://apiportal.koreainvestment.com/about-open-api), so naive
    re-issuance on every call gets the caller throttled or banned.
    We therefore:
        1. Cache the token to disk under data/kis/ keyed by mode
           (paper vs real) so it survives process restarts.
        2. Reuse the cached token until 5 minutes before its stated
           expiry — the safety margin guards against clock skew and
           in-flight requests crossing the boundary.
        3. Serialize concurrent issuance through a thread-lock so
           multi-threaded callers don't burn the rate limit racing
           each other.

What this module does NOT provide:
    - Hashkey generation (POST /uapi/hashkey). That belongs in a
      separate request-signing helper; auth is concerned only with
      bearer-token lifecycle.
    - WebSocket approval_key issuance (POST /oauth2/Approval). Same
      reason — different credential, different lifecycle.
    - HTTP request helpers that consume the token. The token is
      exposed via `auth.access_token` / `auth.auth_header()`; the
      KIS API client will live in src/kis/client.py.

Endpoints:
    Real:  https://openapi.koreainvestment.com:9443
    Paper: https://openapivts.koreainvestment.com:29443
    Issue token:  POST /oauth2/tokenP
    Revoke token: POST /oauth2/revokeP
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.config import PROJECT_ROOT, get_kis_settings
from src.utils.logger import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REAL_HOST = "https://openapi.koreainvestment.com:9443"
PAPER_HOST = "https://openapivts.koreainvestment.com:29443"

ISSUE_PATH = "/oauth2/tokenP"
REVOKE_PATH = "/oauth2/revokeP"

# KIS spec: token lives 24h. We refresh slightly early to avoid a request
# racing past the stated expiry while in flight.
TOKEN_EXPIRY_SAFETY_MARGIN = timedelta(minutes=5)

# Network timeouts. Token issuance is small + cheap, so a generous
# overall budget with a short connect timeout is fine.
HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)

# Token cache lives inside the project (gitignored via .gitignore's
# data/ entry). Keep mode in the filename so paper/real don't collide.
TOKEN_CACHE_DIR = PROJECT_ROOT / "data" / "kis"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class KISAuthError(RuntimeError):
    """Raised when KIS auth fails in a way the caller needs to know about.

    Distinct from network errors (which tenacity retries internally) — by
    the time this is raised, retries are exhausted or the failure is a
    credential/configuration problem that retrying cannot fix.
    """


# ---------------------------------------------------------------------------
# KISAuth
# ---------------------------------------------------------------------------


class KISAuth:
    """Manage a single KIS OAuth access_token for one (mode, app_key) pair.

    Typical usage:
        auth = get_kis_auth()           # singleton, picks up .env
        token = auth.access_token       # str — issues if needed, else cached
        headers = auth.auth_header()    # dict — drop into any KIS REST call

    The class is safe to share across threads. Token issuance is serialized
    via an internal lock to avoid hammering the per-minute rate limit.
    """

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        mode: str = "paper",
        cache_dir: Path | None = None,
    ) -> None:
        if not app_key or not app_secret:
            raise KISAuthError(
                "KIS app_key/app_secret are empty. Set KIS_APP_KEY and "
                "KIS_APP_SECRET in .env (issued at "
                "https://apiportal.koreainvestment.com)."
            )
        if mode not in ("paper", "real"):
            raise KISAuthError(
                f"Invalid KIS mode: {mode!r}. Must be 'paper' or 'real'."
            )

        self.app_key = app_key
        self.app_secret = app_secret
        self.mode = mode
        self.host = PAPER_HOST if mode == "paper" else REAL_HOST

        self._cache_dir = cache_dir or TOKEN_CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path = self._cache_dir / f"token_{mode}.json"

        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: datetime | None = None

        # Try to hydrate from disk so process restarts don't re-issue.
        self._load_cached_token()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def access_token(self) -> str:
        """Return a valid access_token, issuing/refreshing as needed."""
        return self.ensure_token()

    def auth_header(self) -> dict[str, str]:
        """Standard headers for KIS REST calls (excluding tr_id, custtype, etc.).

        Caller is expected to merge in endpoint-specific headers like:
            tr_id, custtype, hashkey
        """
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }

    def ensure_token(self, force_refresh: bool = False) -> str:
        """Return a usable token. Issue a new one if missing/expired/forced.

        Thread-safe: only one issuance can be in flight at a time across
        callers sharing this instance.
        """
        with self._lock:
            if not force_refresh and self._token and not self._is_expiring_soon():
                return self._token

            if force_refresh:
                logger.info(f"KIS auth ({self.mode}): forced token refresh")
            elif self._token is None:
                logger.info(f"KIS auth ({self.mode}): no cached token, issuing")
            else:
                logger.info(
                    f"KIS auth ({self.mode}): cached token expired or near "
                    f"expiry (expires_at={self._expires_at!s}), issuing"
                )

            token, expires_at = self._issue_token()
            self._token = token
            self._expires_at = expires_at
            self._save_cached_token(token, expires_at)
            logger.success(
                f"KIS auth ({self.mode}): token issued, valid until "
                f"{expires_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}"
            )
            return token

    def revoke(self) -> None:
        """Revoke the currently held token via /oauth2/revokeP and clear cache.

        Per KIS docs, revoke takes the bearer token as the `token` field in
        the body. After revocation we drop both in-memory and on-disk state
        so the next ensure_token() will issue a fresh one.
        """
        with self._lock:
            if not self._token:
                logger.info(f"KIS auth ({self.mode}): nothing to revoke")
                return
            try:
                self._revoke_token(self._token)
                logger.success(f"KIS auth ({self.mode}): token revoked")
            except Exception as e:
                # Revocation failures are non-fatal — KIS may have already
                # invalidated the token (e.g. because we issued a newer one).
                # Log and proceed to clear local state regardless.
                logger.warning(
                    f"KIS auth ({self.mode}): revoke call failed ({e}); "
                    "clearing local cache anyway"
                )
            finally:
                self._token = None
                self._expires_at = None
                self._cache_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _is_expiring_soon(self) -> bool:
        if self._expires_at is None:
            return True
        now = datetime.now(timezone.utc)
        return now + TOKEN_EXPIRY_SAFETY_MARGIN >= self._expires_at

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (httpx.TransportError, httpx.HTTPStatusError)
        ),
        reraise=True,
    )
    def _issue_token(self) -> tuple[str, datetime]:
        """POST /oauth2/tokenP and return (access_token, expires_at_utc).

        KIS responds with:
            {
              "access_token": "...",
              "access_token_token_expired": "YYYY-MM-DD HH:MM:SS",  # KST
              "token_type": "Bearer",
              "expires_in": 86400
            }

        We prefer `expires_in` (relative seconds) over the timestamp string
        because it sidesteps timezone-parsing pitfalls on the field name
        `access_token_token_expired`, which is local-clock KST without an
        offset.
        """
        url = f"{self.host}{ISSUE_PATH}"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        headers = {"content-type": "application/json; charset=utf-8"}

        issued_at = datetime.now(timezone.utc)
        try:
            resp = httpx.post(
                url, headers=headers, json=body, timeout=HTTP_TIMEOUT
            )
        except httpx.TransportError as e:
            logger.error(f"KIS auth: transport error during issue: {e}")
            raise

        # KIS returns 4xx with a JSON body explaining the cause (bad key,
        # rate-limit, etc.). Surface the message rather than a bare status.
        if resp.status_code >= 400:
            err_payload = self._safe_json(resp)
            msg = (
                err_payload.get("error_description")
                or err_payload.get("msg1")
                or resp.text
            )
            raise KISAuthError(
                f"KIS token issue failed [{resp.status_code}]: {msg}"
            )

        payload = resp.json()
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")

        if not token:
            raise KISAuthError(
                f"KIS token response missing access_token: {payload}"
            )
        if not isinstance(expires_in, int) or expires_in <= 0:
            # Fallback: spec guarantees 24h, so use that if expires_in is
            # malformed rather than failing the whole flow.
            logger.warning(
                f"KIS auth: expires_in missing/invalid ({expires_in!r}), "
                "falling back to 24h"
            )
            expires_in = 86400

        expires_at = issued_at + timedelta(seconds=expires_in)
        return token, expires_at

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    def _revoke_token(self, token: str) -> None:
        url = f"{self.host}{REVOKE_PATH}"
        body = {
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "token": token,
        }
        headers = {"content-type": "application/json; charset=utf-8"}
        resp = httpx.post(
            url, headers=headers, json=body, timeout=HTTP_TIMEOUT
        )
        if resp.status_code >= 400:
            err_payload = self._safe_json(resp)
            msg = (
                err_payload.get("error_description")
                or err_payload.get("msg1")
                or resp.text
            )
            raise KISAuthError(
                f"KIS token revoke failed [{resp.status_code}]: {msg}"
            )

    @staticmethod
    def _safe_json(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ---- cache I/O ----

    def _load_cached_token(self) -> None:
        if not self._cache_path.exists():
            return
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            token = data["access_token"]
            expires_at = datetime.fromisoformat(data["expires_at"])
            # Guard against hand-edited or version-skew cache files.
            if data.get("app_key_fingerprint") != self._app_key_fp():
                logger.info(
                    f"KIS auth ({self.mode}): cached token belongs to a "
                    "different app_key, ignoring"
                )
                return
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            logger.warning(
                f"KIS auth ({self.mode}): cache file corrupt ({e}), ignoring"
            )
            return

        if expires_at <= datetime.now(timezone.utc) + TOKEN_EXPIRY_SAFETY_MARGIN:
            logger.info(
                f"KIS auth ({self.mode}): cached token expired, will re-issue"
            )
            return

        self._token = token
        self._expires_at = expires_at
        logger.info(
            f"KIS auth ({self.mode}): loaded cached token, valid until "
            f"{expires_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )

    def _save_cached_token(self, token: str, expires_at: datetime) -> None:
        payload = {
            "access_token": token,
            "expires_at": expires_at.isoformat(),
            "mode": self.mode,
            "app_key_fingerprint": self._app_key_fp(),
        }
        # Atomic-ish write: same directory, then replace. Avoids leaving a
        # truncated file on crash.
        tmp = self._cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._cache_path)

    def _app_key_fp(self) -> str:
        """Short fingerprint of the app_key — enough to detect a swap
        without storing the full key in the cache file."""
        return f"{self.app_key[:6]}…{self.app_key[-4:]}" if self.app_key else ""


# ---------------------------------------------------------------------------
# Module-level convenience: process-wide singleton
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_kis_auth() -> KISAuth:
    """Return a process-wide KISAuth built from .env settings.

    Cached, so all callers in the same process share a single token and
    a single issuance lock. Use KISAuth(...) directly if you need to
    instantiate against non-default credentials (e.g. testing).
    """
    settings = get_kis_settings()
    return KISAuth(
        app_key=settings.kis_app_key,
        app_secret=settings.kis_app_secret,
        mode=settings.kis_mode,
    )


__all__ = [
    "KISAuth",
    "KISAuthError",
    "get_kis_auth",
    "REAL_HOST",
    "PAPER_HOST",
]
