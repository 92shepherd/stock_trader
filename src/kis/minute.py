"""KIS Open API — 국내주식 분봉 조회 래퍼.

Endpoint: GET /uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice
TR ID:    FHKST03010200

특성:
    - 한 번에 최대 30개의 1분봉 반환 (FID_INPUT_HOUR_1 기준 이전 데이터).
    - 응답은 최신→과거 역순. stck_bsop_date + stck_cntg_hour 로 날짜+시각 구성.
    - FID_PW_DATA_INCU_YN="Y" → 날짜 경계를 넘어 이전 영업일로 계속 페이징 가능.
    - 조회 가능 기간: 최근 30거래일 (KIS API 제약).

페이징 전략 (fetch_day):
    - cursor = "153000" 부터 역방향으로 30건씩 호출.
    - 각 페이지에서 target_date 해당 봉만 수집.
    - 가장 오래된 봉의 날짜가 target_date 이전이면 완료.
    - 거래 시작(09:00) 이전에 도달하면 완료.

주의:
    - target_date 가 오래될수록 (최대 30거래일) 중간 날짜들을 페이징으로 건너뛰어야 해
      API 호출 횟수가 비례해 증가함.
    - 실전/모의 공통 TR ID 사용 (시세 조회).
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.kis.auth import KISAuth, get_kis_auth
from src.kis.quotations import KISQuotationsError
from src.utils.logger import logger

# ---------------------------------------------------------------------------
# Endpoint / TR constants
# ---------------------------------------------------------------------------

MINUTE_CHART_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
TR_MINUTE_CHART = "FHKST03010200"

# KST 기준 거래 시작/종료 HHMMSS
MARKET_OPEN_STR = "090000"
MARKET_CLOSE_STR = "153000"

# 호출 1회당 최대 반환 봉 수
MAX_BARS_PER_CALL = 30

HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


# ---------------------------------------------------------------------------
# KISMinute
# ---------------------------------------------------------------------------


class KISMinute:
    """1분봉 조회 — 날짜+종목 단위 페이징."""

    def __init__(self, auth: KISAuth) -> None:
        self.auth = auth
        self.host = auth.host

    def fetch_day(
        self,
        symbol: str,
        target_date: date,
        *,
        request_delay: float = 0.07,
    ) -> list[dict[str, Any]]:
        """target_date 하루치 1분봉 전체를 수집해서 반환.

        Returns:
            시간 오름차순(oldest first) 정렬된 raw bar dict 리스트.
            target_date 이외의 봉은 포함하지 않음.

            각 봉: stck_bsop_date, stck_cntg_hour, stck_prpr(close),
                   stck_oprc, stck_hgpr, stck_lwpr, cntg_vol,
                   acml_vol, acml_tr_pbmn
        """
        target_str = target_date.strftime("%Y%m%d")
        all_bars: list[dict[str, Any]] = []
        cursor = MARKET_CLOSE_STR  # 장 마감부터 역방향 페이징

        while True:
            page = self._call(symbol, cursor)
            time.sleep(request_delay)

            if not page:
                break

            # target_date 에 해당하는 봉만 수집
            day_bars = [b for b in page if b.get("stck_bsop_date") == target_str]
            all_bars.extend(day_bars)

            # 가장 오래된 봉의 날짜 확인
            oldest_date_str = page[-1].get("stck_bsop_date", "")
            oldest_hour_str = page[-1].get("stck_cntg_hour", "")

            # target_date 이전 날짜까지 내려갔으면 완료
            if oldest_date_str and oldest_date_str < target_str:
                break

            # 거래 시작(09:00) 이전에 도달하면 완료
            if oldest_date_str == target_str and oldest_hour_str <= MARKET_OPEN_STR:
                break

            # 다음 페이지 cursor 설정: 가장 오래된 봉보다 1분 이전
            next_cursor = self._prev_minute(oldest_hour_str)
            if next_cursor is None:
                break
            cursor = next_cursor

        # 시간 오름차순 정렬
        all_bars.sort(
            key=lambda b: (b.get("stck_bsop_date", ""), b.get("stck_cntg_hour", ""))
        )
        return all_bars

    @staticmethod
    def _prev_minute(hour_str: str) -> str | None:
        """HHMMSS 문자열에서 1분 뺀 HHMMSS 반환. 09:00 미만이면 None."""
        if len(hour_str) != 6:
            return None
        try:
            h, m, s = int(hour_str[:2]), int(hour_str[2:4]), int(hour_str[4:])
            dt = datetime(2000, 1, 1, h, m, s) - timedelta(minutes=1)
            if dt.hour < 9:
                return None
            return dt.strftime("%H%M%S")
        except (ValueError, OverflowError):
            return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    def _call(self, symbol: str, hour: str) -> list[dict[str, Any]]:
        """단일 분봉 조회 호출. transport 오류만 재시도."""
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_HOUR_1": hour,
            "FID_PW_DATA_INCU_YN": "Y",
        }
        headers = {
            **self.auth.auth_header(),
            "tr_id": TR_MINUTE_CHART,
            "custtype": "P",
        }
        url = f"{self.host}{MINUTE_CHART_PATH}"

        try:
            resp = httpx.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
        except httpx.TransportError as e:
            logger.error(f"[kis_minute] transport error: {e}")
            raise

        if resp.status_code >= 400:
            raise KISQuotationsError(
                f"KIS HTTP {resp.status_code} [{TR_MINUTE_CHART}]: {resp.text[:300]}"
            )

        try:
            payload = resp.json()
        except ValueError as e:
            raise KISQuotationsError(
                f"KIS {TR_MINUTE_CHART}: response is not JSON: {e}"
            ) from e

        rt_cd = payload.get("rt_cd")
        if rt_cd != "0":
            msg_cd = payload.get("msg_cd", "")
            msg1 = payload.get("msg1", "").strip()
            raise KISQuotationsError(
                f"KIS {TR_MINUTE_CHART} [{msg_cd}]: {msg1 or str(payload)[:200]}"
            )

        output2 = payload.get("output2") or []
        return output2 if isinstance(output2, list) else []


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_singleton: KISMinute | None = None


def get_kis_minute() -> KISMinute:
    """프로세스 공유 KISMinute 싱글톤."""
    global _singleton
    if _singleton is None:
        _singleton = KISMinute(auth=get_kis_auth())
    return _singleton


__all__ = [
    "KISMinute",
    "get_kis_minute",
    "MARKET_OPEN_STR",
    "MARKET_CLOSE_STR",
    "MAX_BARS_PER_CALL",
]
