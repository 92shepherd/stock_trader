"""KIS Open API — 국내주식 계좌 기본 정보 조회.

Purpose:
    KIS doesn't expose a single "account info" endpoint. Two distinct
    REST calls together give you the practical "account snapshot":

        1. inquire-balance      → holdings + cash + valuation
                                  (TR_ID: TTTC8434R real / VTTC8434R paper)
        2. inquire-psbl-order   → buyable cash for a given symbol/price
                                  (TR_ID: TTTC8908R real / VTTC8908R paper)

    Both are GET requests. Both are paginated (KIS calls it "연속조회"):
    when a response sets `tr_cont = "M"` (more pages exist), the next
    call must echo the returned `ctx_area_fk100` / `ctx_area_nk100`
    cursor values back as request params and pass `tr_cont = "N"` as a
    header. balance is the one that meaningfully paginates (>20 holdings
    in paper, >50 in real); psbl-order does not, but we still implement
    the loop generically for safety.

What this module exposes:
    KISAccount.inquire_balance(...)        → (holdings_df, summary_df)
    KISAccount.inquire_psbl_order(...)     → DataFrame (1 row)
    KISAccount.summary_kor(summary_df)     → 3-column 세로 DataFrame
                                             (원본코드 / 한글명 / 값)
    get_kis_account()                      → process-wide singleton

    Plus three KOR-name mapping dicts (raw_field → 한글명):
        BALANCE_HOLDINGS_KOR
        BALANCE_SUMMARY_KOR
        PSBL_ORDER_KOR

    Use these with `df.rename(columns=...)` if you want fully Korean
    DataFrames. For programmatic access (자동매매 등) keep the raw
    English keys — that's what the rest of the codebase will look for.

Design notes:
    - Headers (tr_id, custtype) are derived from the auth mode so the
      caller never has to remember "VTTC..." vs "TTTC...". Mode comes
      from KISAuth, which already encodes paper/real.
    - All numeric-looking output fields stay as strings on the wire;
      we DON'T eagerly cast to int/float because (a) some columns
      legitimately carry signed strings or empty values, and (b) the
      caller may want pandas type inference downstream. Cast at the
      call site if you need numeric ops.
    - Retries are limited to transport errors. KIS business errors
      (rt_cd != "0") raise immediately — retrying a "계좌 비밀번호
      불일치" or "거래시간이 아닙니다" is just noise.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

import httpx
import pandas as pd
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.config import get_kis_settings
from src.kis.auth import KISAuth, get_kis_auth
from src.utils.logger import logger

# ---------------------------------------------------------------------------
# Endpoint paths & TR IDs
# ---------------------------------------------------------------------------

BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
PSBL_ORDER_PATH = "/uapi/domestic-stock/v1/trading/inquire-psbl-order"

# TR_ID prefix:
#   real:  T (실전)  →  TTTC8434R / TTTC8908R
#   paper: V (모의)  →  VTTC8434R / VTTC8908R
TR_ID_BALANCE = {"real": "TTTC8434R", "paper": "VTTC8434R"}
TR_ID_PSBL_ORDER = {"real": "TTTC8908R", "paper": "VTTC8908R"}

# Pagination safety cap. KIS pages 20 (paper) / 50 (real) holdings at a
# time; a single account holding >5,000 stocks is effectively impossible.
# This cap is purely a defense against an infinite loop from a buggy
# cursor echo.
MAX_PAGES = 100

HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)


# ---------------------------------------------------------------------------
# Korean-name mappings for response fields
# ---------------------------------------------------------------------------
#
# Source: KIS Developers 공식 API 문서 (apiportal.koreainvestment.com)
#   - 주식잔고조회      (TTTC8434R / VTTC8434R)
#   - 주식매수가능조회  (TTTC8908R / VTTC8908R)
#
# Some fields appear in real-trading responses but are blank/zero in paper.
# The mapping dicts below are reference-only; missing keys in an actual
# response are silently skipped by `_map_kor()`.

BALANCE_HOLDINGS_KOR: dict[str, str] = {
    # 종목 식별
    "pdno": "상품번호(종목코드)",
    "prdt_name": "상품명(종목명)",
    "trad_dvsn_name": "매매구분명",
    "bfdy_buy_qty": "전일매수수량",
    "bfdy_sll_qty": "전일매도수량",
    "thdt_buyqty": "금일매수수량",
    "thdt_sll_qty": "금일매도수량",
    # 보유 / 평가
    "hldg_qty": "보유수량",
    "ord_psbl_qty": "주문가능수량",
    "pchs_avg_pric": "매입평균가격",
    "pchs_amt": "매입금액",
    "prpr": "현재가",
    "evlu_amt": "평가금액",
    "evlu_pfls_amt": "평가손익금액",
    "evlu_pfls_rt": "평가손익율",
    "evlu_erng_rt": "평가수익율",
    # 대출 / 융자 (단타 계좌에는 보통 비어 있음)
    "loan_dt": "대출일자",
    "loan_amt": "대출금액",
    "stln_slng_chgs": "대주매각대금",
    "expd_dt": "만기일자",
    "fltt_rt": "등락율",
    "bfdy_cprs_icdc": "전일대비증감",
    # 기타
    "item_mgna_rt_name": "종목증거금율명",
    "grta_rt_name": "보증금율명",
    "sbst_pric": "대용가격",
    "stck_loan_unpr": "주식대출단가",
}

BALANCE_SUMMARY_KOR: dict[str, str] = {
    # 예수금 (Cash)
    "dnca_tot_amt": "예수금총금액",
    "nxdy_excc_amt": "익일정산금액(D+1예수금)",
    "prvs_rcdl_excc_amt": "가수도정산금액(D+2예수금)",
    "cma_evlu_amt": "CMA평가금액",
    # 매수/매도 흐름
    "bfdy_buy_amt": "전일매수금액",
    "thdt_buy_amt": "금일매수금액",
    "nxdy_auto_rdpt_amt": "익일자동상환금액",
    "bfdy_sll_amt": "전일매도금액",
    "thdt_sll_amt": "금일매도금액",
    "d2_auto_rdpt_amt": "D+2자동상환금액",
    "bfdy_tlex_amt": "전일제비용금액",
    "thdt_tlex_amt": "금일제비용금액",
    "tot_loan_amt": "총대출금액",
    # 평가 (Valuation)
    "scts_evlu_amt": "유가평가금액",
    "tot_evlu_amt": "총평가금액(예수금+유가평가)",
    "nass_amt": "순자산금액",
    "fncg_gld_auto_rdpt_yn": "융자금자동상환여부",
    "pchs_amt_smtl_amt": "매입금액합계금액",
    "evlu_amt_smtl_amt": "평가금액합계금액",
    "evlu_pfls_smtl_amt": "평가손익합계금액",
    "tot_stln_slng_chgs": "총대주매각대금",
    "bfdy_tot_asst_evlu_amt": "전일총자산평가금액",
    "asst_icdc_amt": "자산증감액",
    "asst_icdc_erng_rt": "자산증감수익율",
}

PSBL_ORDER_KOR: dict[str, str] = {
    "ord_psbl_cash": "주문가능현금",
    "ord_psbl_sbst": "주문가능대용",
    "ruse_psbl_amt": "재사용가능금액",
    "fund_rpch_chgs": "펀드환매대금",
    "psbl_qty_calc_unpr": "가능수량계산단가",
    "nrcvb_buy_amt": "미수없는매수금액",
    "nrcvb_buy_qty": "미수없는매수수량",
    "max_buy_amt": "최대매수금액",
    "max_buy_qty": "최대매수수량",
    "cma_evlu_amt": "CMA평가금액",
    "ovrs_re_use_amt_wcrc": "해외재사용금액원화",
    "ord_psbl_frcr_amt_wcrc": "주문가능외화금액원화",
}


def _map_kor(field: str, mapping: dict[str, str]) -> str:
    """Look up the KOR name; fall back to the raw field if unknown.

    Used by `summary_kor()` to gracefully handle KIS schema additions
    we haven't catalogued yet — better to show the raw code than to
    drop a row.
    """
    return mapping.get(field, f"(미매핑: {field})")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class KISAccountError(RuntimeError):
    """Raised when KIS returns rt_cd != "0" or the response shape is unusable.

    Distinct from network errors (which tenacity handles internally) — by
    the time this surfaces, the call reached KIS but KIS rejected it for
    a business reason (bad credentials, off-hours, etc.) that retrying
    will not fix.
    """


# ---------------------------------------------------------------------------
# KISAccount
# ---------------------------------------------------------------------------


class KISAccount:
    """Domestic-stock account queries against KIS Open API.

    Typical usage:
        acct = get_kis_account()
        holdings, summary = acct.inquire_balance()
        psbl = acct.inquire_psbl_order(stock_code="005930", price=70000)

        # 사람이 읽을 때:
        kor_view = acct.summary_kor(summary)            # 세로 DataFrame
        holdings_kor = holdings.rename(columns=BALANCE_HOLDINGS_KOR)
    """

    def __init__(
        self,
        auth: KISAuth,
        account_no: str,
        account_product: str,
    ) -> None:
        if not account_no or not account_product:
            raise KISAccountError(
                "KIS_ACCOUNT_NO / KIS_ACCOUNT_PRODUCT are empty. Fill "
                "them in .env (계좌번호 앞 8자리 / 뒤 2자리)."
            )
        self.auth = auth
        self.account_no = account_no
        self.account_product = account_product
        self.mode = auth.mode  # "paper" | "real"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def inquire_balance(
        self,
        *,
        afhr_flpr_yn: str = "N",
        inqr_dvsn: str = "02",
        unpr_dvsn: str = "01",
        fund_sttl_icld_yn: str = "N",
        fncg_amt_auto_rdpt_yn: str = "N",
        prcs_dvsn: str = "00",
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """주식잔고조회 (inquire-balance).

        Returns:
            (holdings_df, summary_df)
                holdings_df — one row per held stock (output1).
                              Empty DataFrame if no holdings.
                summary_df  — one row, account-level totals (output2):
                              예수금총액(dnca_tot_amt), 평가금액(evlu_amt_smtl_amt),
                              총평가금액(tot_evlu_amt), 매입금액(pchs_amt_smtl_amt),
                              평가손익(evlu_pfls_smtl_amt) 등.

            Both DataFrames keep raw KIS field names as columns. Use
            `BALANCE_HOLDINGS_KOR` / `BALANCE_SUMMARY_KOR` mapping dicts
            with `df.rename(columns=...)`, or `summary_kor(summary_df)`
            for a human-readable vertical view.

        Notes on params (KIS-defined; defaults match the typical "show
        me my account" intent):
            inqr_dvsn   "02" 종목별 / "01" 대출일별
            unpr_dvsn   "01" 기본값
            prcs_dvsn   "00" 전일매매포함 / "01" 전일매매미포함
            afhr_flpr_yn   "N" 시간외단일가 미반영
        """
        params = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_product,
            "AFHR_FLPR_YN": afhr_flpr_yn,
            "OFL_YN": "",
            "INQR_DVSN": inqr_dvsn,
            "UNPR_DVSN": unpr_dvsn,
            "FUND_STTL_ICLD_YN": fund_sttl_icld_yn,
            "FNCG_AMT_AUTO_RDPT_YN": fncg_amt_auto_rdpt_yn,
            "PRCS_DVSN": prcs_dvsn,
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        all_holdings: list[dict[str, Any]] = []
        last_summary: list[dict[str, Any]] = []

        for page_idx, payload in enumerate(
            self._paginate(BALANCE_PATH, TR_ID_BALANCE[self.mode], params)
        ):
            output1 = payload.get("output1") or []
            output2 = payload.get("output2") or []
            if isinstance(output1, list):
                all_holdings.extend(output1)
            # output2 (summary) is repeated on every page; the last page
            # carries the canonical totals (KIS recomputes it cumulatively).
            if isinstance(output2, list) and output2:
                last_summary = output2

            logger.debug(
                f"KIS balance page {page_idx + 1}: "
                f"+{len(output1) if isinstance(output1, list) else 0} holdings"
            )

        holdings_df = pd.DataFrame(all_holdings)
        summary_df = pd.DataFrame(last_summary)

        logger.info(
            f"KIS inquire_balance ({self.mode}): "
            f"{len(holdings_df)} holdings, "
            f"summary_rows={len(summary_df)}"
        )
        return holdings_df, summary_df

    def inquire_psbl_order(
        self,
        *,
        stock_code: str = "005930",
        price: int = 0,
        ord_dvsn: str = "01",
        cma_evlu_amt_icld_yn: str = "N",
        ovrs_icld_yn: str = "N",
    ) -> pd.DataFrame:
        """매수가능조회 (inquire-psbl-order).

        Args:
            stock_code: 상품번호(종목코드, 6자리). KIS는 필수 필드로 받지만
                "현금잔고만 보고 싶다"는 경우라도 더미가 필요하므로
                기본값으로 삼성전자(005930)를 사용한다. 종목별 매수가능
                수량(max_buy_qty)이 필요하면 실제 종목코드로 교체한다.
            price: 주문단가. 시장가(`ord_dvsn="01"`) 기준 0이면 KIS가
                내부적으로 현재가를 사용한다.
            ord_dvsn: 주문구분. "00" 지정가 / "01" 시장가 / 기타 — API문서 참조.
            cma_evlu_amt_icld_yn: CMA평가금액 포함여부.
            ovrs_icld_yn: 해외포함여부.

        Returns:
            1-row DataFrame from `output`. Use `PSBL_ORDER_KOR` for
            Korean column names. Key fields:
                ord_psbl_cash      주문가능현금
                nrcvb_buy_amt      미수없는매수금액
                max_buy_amt        최대매수금액
                max_buy_qty        최대매수수량
                psbl_qty_calc_unpr 가능수량계산단가
        """
        params = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_product,
            "PDNO": stock_code,
            "ORD_UNPR": str(int(price)),
            "ORD_DVSN": ord_dvsn,
            "CMA_EVLU_AMT_ICLD_YN": cma_evlu_amt_icld_yn,
            "OVRS_ICLD_YN": ovrs_icld_yn,
        }

        # psbl-order is logically a single page, but we route through the
        # same paginator for consistent error handling. In practice the
        # loop runs exactly once.
        rows: list[dict[str, Any]] = []
        for payload in self._paginate(
            PSBL_ORDER_PATH, TR_ID_PSBL_ORDER[self.mode], params
        ):
            output = payload.get("output")
            if isinstance(output, dict):
                rows.append(output)
            elif isinstance(output, list):
                rows.extend(output)

        df = pd.DataFrame(rows)
        logger.info(
            f"KIS inquire_psbl_order ({self.mode}): "
            f"stock_code={stock_code}, ord_dvsn={ord_dvsn}, rows={len(df)}"
        )
        return df

    # ------------------------------------------------------------------
    # Korean-view helpers
    # ------------------------------------------------------------------

    @staticmethod
    def summary_kor(
        df: pd.DataFrame,
        mapping: dict[str, str] | None = None,
    ) -> pd.DataFrame:
        """Convert a 1-row "summary"-shape DataFrame to a vertical 3-column view.

        Works for both `summary_df` (from inquire_balance) and the 1-row
        output of `inquire_psbl_order`. The output is intended for human
        consumption (printing / Jupyter display), not for downstream
        programmatic access — keep the original DataFrame for that.

        Args:
            df: single-row DataFrame. If empty, returns an empty
                3-column DataFrame.
            mapping: raw_field → 한글명 dict. Defaults to the union of
                `BALANCE_SUMMARY_KOR` and `PSBL_ORDER_KOR` so this works
                regardless of which call produced the row.

        Returns:
            DataFrame with columns: ["원본코드", "한글명", "값"].
            One row per field present in the input. Field order matches
            the input column order (KIS preserves a stable ordering).
        """
        if df is None or df.empty:
            return pd.DataFrame(columns=["원본코드", "한글명", "값"])

        if len(df) > 1:
            logger.warning(
                f"summary_kor: input has {len(df)} rows, only the first "
                "is rendered. Pass holdings rows individually if needed."
            )

        m = mapping if mapping is not None else {**BALANCE_SUMMARY_KOR, **PSBL_ORDER_KOR}

        row = df.iloc[0]
        records = [
            {"원본코드": col, "한글명": _map_kor(col, m), "값": row[col]}
            for col in df.columns
        ]
        return pd.DataFrame.from_records(records)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _paginate(
        self,
        path: str,
        tr_id: str,
        params: dict[str, str],
    ):
        """Yield each page's JSON payload, handling KIS 연속조회.

        Cursor protocol:
            - First call:  tr_cont header = "" (or absent); CTX_AREA_*
              params = "".
            - If response header `tr_cont` == "F"/"M" → more pages.
              Send next request with header `tr_cont = "N"` and update
              CTX_AREA_FK100 / CTX_AREA_NK100 from the response body.
            - If response header `tr_cont` in ("D", "E", "") → done.
        """
        url = f"{self.auth.host}{path}"
        # Working copy — we mutate the cursor fields between pages.
        request_params = dict(params)
        tr_cont_in = ""

        for page in range(MAX_PAGES):
            payload, tr_cont_out = self._do_get(url, tr_id, request_params, tr_cont_in)
            yield payload

            if tr_cont_out not in ("F", "M"):
                return  # last page

            # Advance cursor for next call.
            request_params["CTX_AREA_FK100"] = payload.get("ctx_area_fk100", "") or ""
            request_params["CTX_AREA_NK100"] = payload.get("ctx_area_nk100", "") or ""
            tr_cont_in = "N"

        logger.warning(
            f"KIS {path}: hit MAX_PAGES={MAX_PAGES}, stopping pagination "
            "(possible cursor bug)."
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(httpx.TransportError),
        reraise=True,
    )
    def _do_get(
        self,
        url: str,
        tr_id: str,
        params: dict[str, str],
        tr_cont_in: str,
    ) -> tuple[dict[str, Any], str]:
        """Single GET. Returns (json_body, tr_cont_response_header).

        Retries on transport errors only; business errors (rt_cd != "0")
        raise KISAccountError immediately.
        """
        headers = {
            **self.auth.auth_header(),
            "tr_id": tr_id,
            "custtype": "P",  # 개인 (법인은 "B")
            "tr_cont": tr_cont_in,
        }

        try:
            resp = httpx.get(
                url, headers=headers, params=params, timeout=HTTP_TIMEOUT
            )
        except httpx.TransportError as e:
            logger.error(f"KIS {url}: transport error: {e}")
            raise

        # KIS still returns 200 for business errors and encodes the
        # outcome in the body's rt_cd / msg1, so we don't gate on
        # status_code alone — but a 4xx/5xx is still a hard failure.
        if resp.status_code >= 400:
            raise KISAccountError(
                f"KIS HTTP {resp.status_code} for tr_id={tr_id}: {resp.text}"
            )

        try:
            payload = resp.json()
        except ValueError as e:
            raise KISAccountError(
                f"KIS {tr_id}: response is not JSON ({e}): {resp.text[:200]}"
            ) from e

        rt_cd = payload.get("rt_cd")
        if rt_cd != "0":
            msg_cd = payload.get("msg_cd", "")
            msg1 = payload.get("msg1", "").strip()
            raise KISAccountError(
                f"KIS {tr_id} business error [{msg_cd}]: {msg1 or payload}"
            )

        # tr_cont in response headers tells us whether more pages remain.
        tr_cont_out = resp.headers.get("tr_cont", "")
        return payload, tr_cont_out


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_kis_account() -> KISAccount:
    """Process-wide KISAccount built from .env settings + shared KISAuth."""
    settings = get_kis_settings()
    return KISAccount(
        auth=get_kis_auth(),
        account_no=settings.kis_account_no,
        account_product=settings.kis_account_product,
    )


__all__ = [
    "KISAccount",
    "KISAccountError",
    "get_kis_account",
    "BALANCE_HOLDINGS_KOR",
    "BALANCE_SUMMARY_KOR",
    "PSBL_ORDER_KOR",
]
