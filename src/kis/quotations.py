"""KIS Open API — 국내주식 시세 호출 래퍼.

This module wraps the read-only "quotations" endpoints needed by the
daily_kis collector:

    1. inquire-daily-itemchartprice (FHKST03010100)
       — 종목별 기간별 일봉 (최대 100건/호출).
         수정주가 / 원주가 선택이 FID_ORG_ADJ_PRC 값으로 제어됨.
            FID_ORG_ADJ_PRC = "0"  → 수정주가반영 (default)
            FID_ORG_ADJ_PRC = "1"  → 원주가 (수정미반영)

    2. inquire-price (FHKST01010100)
       — 종목 현재가 스냅샷. 시총/PER/PBR/EPS/BPS/외국인순매수 등.
         backfill에는 쓸 수 없고(과거값 제공 X), "오늘자 스냅샷"을 daily_prices의
         end_date(보통 today) 행에 채우는 용도로만 쓴다.

Design notes:
    - 인증 헤더는 KISAuth에서 넘겨받아 재사용. tr_id는 시세 조회이므로
      실전/모의 구분 없이 동일 값.
    - inquire-daily-itemchartprice의 응답은 output1(종목 메타)과
      output2(일별 시세)가 따로 온다. 일봉 수집은 output2만 쓴다.
    - 모든 수치 필드는 문자열로 온다. 캡스팅은 호출자(collector)에서.
    - rate limit: 실전 20rps / 모의 5rps. 이 모듈은 단일 호출만 담당하고,
      전체 보쇼 간격 제어는 collector 측에서 request_delay로 조절.
"""
from __future__ import annotations

from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.kis.auth import KISAuth, get_kis_auth
from src.utils.logger import logger

# ---------------------------------------------------------------------------
# Endpoint paths & TR IDs
# ---------------------------------------------------------------------------

DAILY_CHART_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
INQUIRE_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"

# 시세 조회는 실전/모의 TR_ID가 같다 (계좌 조회는 T/V 분기되지만).
TR_DAILY_CHART = "FHKST03010100"
TR_INQUIRE_PRICE = "FHKST01010100"

# FID_PERIOD_DIV_CODE
PERIOD_DAILY = "D"
PERIOD_WEEKLY = "W"
PERIOD_MONTHLY = "M"
PERIOD_YEARLY = "Y"

# FID_ORG_ADJ_PRC — KIS 문서 기준: 0=수정주가반영, 1=수정주가미반영(원주가).
ADJ_ADJUSTED = "0"   # 수정주가
ADJ_RAW = "1"        # 원주가

# 한 번의 호출당 최대 영업일 수 (KIS 문서에 명시된 제약).
MAX_DAYS_PER_CALL = 100

HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class KISQuotationsError(RuntimeError):
    """Raised when a quotations endpoint returns rt_cd != "0".

    Distinct from network errors (handled by tenacity). When this fires,
    the call reached KIS but the business response was a failure that
    retrying won't fix (bad symbol, bad date range, off-hours block, etc.).
    """


# ---------------------------------------------------------------------------
# KISQuotations
# ---------------------------------------------------------------------------


class KISQuotations:
    """Read-only domestic-stock market data via KIS Open API.

    Typical usage:
        q = get_kis_quotations()
        meta, bars = q.inquire_daily_itemchartprice(
            symbol="005930", start="20250101", end="20250410",
            adjusted=True,
        )
        snapshot = q.inquire_price(symbol="005930")
    """

    def __init__(self, auth: KISAuth) -> None:
        self.auth = auth
        self.host = auth.host
        self.mode = auth.mode

    # ------------------------------------------------------------------
    # inquire-daily-itemchartprice
    # ------------------------------------------------------------------

    def inquire_daily_itemchartprice(
        self,
        *,
        symbol: str,
        start: str,
        end: str,
        adjusted: bool = True,
        market_div_code: str = "J",
        period_code: str = PERIOD_DAILY,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """국내주식 기간별 시세(일/주/월/년) — 단일 호출.

        Args:
            symbol: 6-digit 종목코드 (ETN은 'Q'로 시작).
            start, end: 'YYYYMMDD' 형식. KIS는 한 번에 최대 100영업일까지
                돌려주므로, 그 이상의 범위는 collector에서 윈도우로 쪼개 쓰자.
            adjusted: True면 수정주가, False면 원주가.
            market_div_code: 'J'=주식/ETF/ETN, 'W'=ELW. 프로젝트는 일반주식만.
            period_code: 'D'/'W'/'M'/'Y'. 일봉 수집은 'D'.

        Returns:
            (output1, output2) 튜플.
              output1: 종목 메타 1개 디타입 (prdy_vrss, hts_kor_isnm 등).
              output2: 날짜별 디타입 리스트(최대 100개).
                       각 항목: stck_bsop_date, stck_oprc, stck_hgpr, stck_lwpr,
                                stck_clpr, acml_vol, acml_tr_pbmn, flng_cls_code, ...
        """
        params = {
            "FID_COND_MRKT_DIV_CODE": market_div_code,
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
            "FID_PERIOD_DIV_CODE": period_code,
            "FID_ORG_ADJ_PRC": ADJ_ADJUSTED if adjusted else ADJ_RAW,
        }
        payload = self._do_get(DAILY_CHART_PATH, TR_DAILY_CHART, params)

        # output1은 dict, output2는 list — 둘 다 누락이 있을 수 있으므로 경화적으로 공혈.
        output1 = payload.get("output1") or {}
        output2 = payload.get("output2") or []
        if not isinstance(output1, dict):
            output1 = {}
        if not isinstance(output2, list):
            output2 = []
        return output1, output2

    # ------------------------------------------------------------------
    # inquire-price
    # ------------------------------------------------------------------

    def inquire_price(
        self,
        *,
        symbol: str,
        market_div_code: str = "J",
    ) -> dict[str, Any]:
        """주식현재가 시세 — 1개 종목 스냅샷.

        Returns:
            output 디타입. 주요 필드:
              stck_prpr        현재가
              acml_vol         누적 거래량
              acml_tr_pbmn     누적 거래대금
              hts_avls         시가총액 (단위: 억원)
              lstn_stcn        상장주수
              per, pbr, eps, bps
              hts_frgn_ehrt    외국인 보유비율
              frgn_ntby_qty    외국인 순매수 수량
              pgtr_ntby_qty    프로그램 순매수 수량
              … (KIS 문서에 전체 목록)

            과거 시점의 키 값을 제공하지 않으므로, daily_prices의
            backfill에는 채워넣을 수 없고 "오늘자" 행에만 기록.
        """
        params = {
            "FID_COND_MRKT_DIV_CODE": market_div_code,
            "FID_INPUT_ISCD": symbol,
        }
        payload = self._do_get(INQUIRE_PRICE_PATH, TR_INQUIRE_PRICE, params)

        output = payload.get("output") or {}
        if not isinstance(output, dict):
            output = {}
        return output

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    def _do_get(
        self,
        path: str,
        tr_id: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        """Single GET. Returns parsed JSON body. Retries transport errors only.

        Business errors (rt_cd != "0") raise KISQuotationsError immediately.
        """
        url = f"{self.host}{path}"
        headers = {
            **self.auth.auth_header(),
            "tr_id": tr_id,
            "custtype": "P",  # 개인
        }

        try:
            resp = httpx.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
        except httpx.TransportError as e:
            logger.error(f"KIS {url}: transport error: {e}")
            raise

        if resp.status_code >= 400:
            raise KISQuotationsError(
                f"KIS HTTP {resp.status_code} for tr_id={tr_id}: {resp.text}"
            )

        try:
            payload = resp.json()
        except ValueError as e:
            raise KISQuotationsError(
                f"KIS {tr_id}: response is not JSON ({e}): {resp.text[:200]}"
            ) from e

        rt_cd = payload.get("rt_cd")
        if rt_cd != "0":
            msg_cd = payload.get("msg_cd", "")
            msg1 = payload.get("msg1", "").strip()
            raise KISQuotationsError(
                f"KIS {tr_id} business error [{msg_cd}]: {msg1 or payload}"
            )

        return payload


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


_singleton: KISQuotations | None = None


def get_kis_quotations() -> KISQuotations:
    """Process-wide KISQuotations using the shared KISAuth singleton.

    Not @lru_cache'd because we want the auth-mode flip in tests to be
    visible without process restart — callers that need stronger caching
    can hold their own KISQuotations instance.
    """
    global _singleton
    if _singleton is None:
        _singleton = KISQuotations(auth=get_kis_auth())
    return _singleton


__all__ = [
    "KISQuotations",
    "KISQuotationsError",
    "get_kis_quotations",
    "MAX_DAYS_PER_CALL",
    "ADJ_ADJUSTED",
    "ADJ_RAW",
]
