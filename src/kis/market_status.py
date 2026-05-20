"""KIS 장 개설 여부 실시간 확인.

inquire-price (FHKST01010100) 응답의 iscd_stat_cls_code 필드로 판단:
    "20"  → 장중 (정규장 거래 시간) → 즉시 True
    "00"  → 장전              ┐
    "40"  → 장후              │ → 즉시 False
    "51"  → 장전 시간외 단일가 │
    "52"  → 장후 시간외 단일가 ┘
    그 외 → KST 시간 기반 fallback (09:00~15:30 평일)

공휴일에는 마지막 거래일 기준 "40" 또는 "00" 이 반환되므로
명시적 비장중 코드가 확인되면 즉시 False로 처리한다.

KST 시간 기반 fallback:
    API가 미정의 코드("55" 등) 또는 오류("")를 반환할 경우
    평일 09:00~15:30 KST 범위를 기준으로 판단한다.
    공휴일에 API가 "00"/"40"을 반환하면 위 명시적 코드로 처리됨.

캐시 전략:
    CACHE_TTL_SECONDS(기본 60s) 동안 API 재호출 없이 캐시 값 반환.
    분당 1회 스케줄러 잡에서 호출될 때 API 호출은 분당 최대 1회.

빠른 사전 필터:
    토/일 → API 호출 없이 즉시 False 반환.
    KST 07:00 이전 · 17:00 이후 → API 호출 없이 즉시 False 반환.
    (명백한 비개장 시간대에 불필요한 API 소모 방지)

오류 처리:
    네트워크·인증 오류 시 KST 시간 기반 fallback 적용 (보수적 처리).
    오류는 WARNING 레벨로 기록하여 운영 가시성 유지.
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from src.utils.logger import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_INQUIRE_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
_TR_INQUIRE_PRICE = "FHKST01010100"

# 삼성전자 — 항상 상장된 대표 종목. 시세 조회로 장 상태 판별.
_STATUS_SYMBOL = "005930"

# 장중 확정 코드
_CODE_TRADING = "20"

# 비장중 확정 코드 — 이 코드가 반환되면 즉시 False
# "00":장전 / "40":장후 / "51":장전시간외단일가 / "52":장후시간외
_CODES_CLOSED = frozenset({"00", "40", "51", "52"})

# 명백히 비개장인 KST 시각 범위 (API 호출 없이 스킵)
_BROAD_START_H = 7   # 07:00 이전
_BROAD_END_H = 17    # 17:00 이후

# 정규장 KST 시간 범위 (fallback 판단용)
_MARKET_OPEN_H, _MARKET_OPEN_M = 9, 0
_MARKET_CLOSE_H, _MARKET_CLOSE_M = 15, 30

CACHE_TTL_SECONDS: float = 60.0

KST = ZoneInfo("Asia/Seoul")

# ---------------------------------------------------------------------------
# Module-level cache (thread-safe for read — 단일 Python GIL 환경)
# ---------------------------------------------------------------------------

_cache: dict = {
    "ts": 0.0,        # monotonic timestamp of last fetch
    "open": False,    # cached result
    "code": "",       # last iscd_stat_cls_code value (for logging)
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_obviously_closed() -> bool:
    """토/일 또는 명백한 비개장 시각(KST 07:00 이전·17:00 이후)이면 True."""
    now = datetime.now(KST)
    if now.weekday() >= 5:  # 토(5), 일(6)
        return True
    if now.hour < _BROAD_START_H or now.hour >= _BROAD_END_H:
        return True
    return False


def _is_market_hour_kst() -> bool:
    """KST 09:00~15:30 평일이면 True. 공휴일은 미처리(API fallback 용도)."""
    now = datetime.now(KST)
    if now.weekday() >= 5:
        return False
    open_dt = now.replace(hour=_MARKET_OPEN_H, minute=_MARKET_OPEN_M, second=0, microsecond=0)
    close_dt = now.replace(hour=_MARKET_CLOSE_H, minute=_MARKET_CLOSE_M, second=0, microsecond=0)
    return open_dt <= now <= close_dt


def _fetch_status_code() -> str:
    """KIS inquire-price로 iscd_stat_cls_code 조회. 오류 시 빈 문자열 반환."""
    # lazy import to avoid circular imports at module load time
    from src.kis.auth import get_kis_auth  # noqa: PLC0415

    auth = get_kis_auth()
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": _STATUS_SYMBOL,
    }
    headers = {
        **auth.auth_header(),
        "tr_id": _TR_INQUIRE_PRICE,
        "custtype": "P",
    }
    try:
        resp = httpx.get(
            f"{auth.host}{_INQUIRE_PRICE_PATH}",
            headers=headers,
            params=params,
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
        )
    except httpx.TransportError as e:
        logger.warning(f"[market_status] network error checking market status: {e}")
        return ""

    if resp.status_code >= 400:
        logger.warning(
            f"[market_status] HTTP {resp.status_code} from inquire-price: "
            f"{resp.text[:200]}"
        )
        return ""

    try:
        payload = resp.json()
    except ValueError:
        logger.warning("[market_status] non-JSON response from inquire-price")
        return ""

    if payload.get("rt_cd") != "0":
        msg = payload.get("msg1", "").strip()
        logger.warning(f"[market_status] inquire-price business error: {msg}")
        return ""

    output = payload.get("output") or {}
    return output.get("iscd_stat_cls_code", "") if isinstance(output, dict) else ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_market_open(*, force_refresh: bool = False) -> bool:
    """현재 KIS 장이 개장 중인지 확인.

    Args:
        force_refresh: True면 캐시를 무시하고 API 재조회.

    Returns:
        True  → 장중 (iscd_stat_cls_code == "20")
        False → 장전·장후·휴장·오류

    Side effects:
        캐시를 갱신하고 결과를 DEBUG 로그로 기록.
        오류 시 WARNING 로그 기록.
    """
    # 명백한 비개장 시간대는 API 없이 바로 스킵
    if _is_obviously_closed():
        return False

    now_ts = time.monotonic()
    if not force_refresh and (now_ts - _cache["ts"]) < CACHE_TTL_SECONDS:
        return _cache["open"]

    code = _fetch_status_code()

    if code == _CODE_TRADING:
        # "20": KIS 명시 장중
        is_open = True
    elif code in _CODES_CLOSED:
        # "00"/"40"/"51"/"52": KIS 명시 비장중 (공휴일 포함)
        is_open = False
    else:
        # 미정의 코드("55" 등) 또는 API 오류("") → KST 시간 기반 fallback
        is_open = _is_market_hour_kst()
        if code:
            logger.warning(
                f"[market_status] unknown iscd_stat_cls_code={code!r}, "
                f"falling back to KST time check → {'open' if is_open else 'closed'}"
            )
        else:
            logger.debug(
                f"[market_status] API error (empty code), "
                f"KST time fallback → {'open' if is_open else 'closed'}"
            )

    _cache["ts"] = now_ts
    _cache["open"] = is_open
    _cache["code"] = code

    logger.debug(
        f"[market_status] iscd_stat_cls_code={code!r} → "
        f"{'장중 (open)' if is_open else '비개장 (closed)'}"
    )
    return is_open


def last_status_code() -> str:
    """마지막으로 캐시된 iscd_stat_cls_code 값 반환. 진단용."""
    return _cache["code"]


__all__ = ["is_market_open", "last_status_code", "CACHE_TTL_SECONDS"]
