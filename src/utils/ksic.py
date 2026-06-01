"""KSIC (한국표준산업분류) 유틸.

DART /api/company.json 의 `induty_code` 필드는 KSIC 10차 개정 기준 5자리
숫자 코드. 본 모듈은 그 코드에서 대분류(섹터) / 중분류명을 파생한다.

레퍼런스:
    통계청 KSIC 10차 개정 (2017.7 적용, 2024 기준 유효).
    대분류는 알파벳 A~U, 우리는 한글 명칭으로 직접 반환.

설계:
    - `tickers` 테이블에는 raw `induty_code` 만 저장하고, 섹터 레벨은
      query-time 에 본 모듈로 파생한다 (정규화 / 중복 제거).
    - 코드 → 명칭 매핑은 KSIC 10차 기준이며, 11차 개정이 나오기 전까지는
      변경 없음. 변경 시 _SECTOR_RANGES 만 갱신하면 됨.

사용 예:
    from src.utils.ksic import induty_code_to_sector
    induty_code_to_sector("26429")  # → '제조업'
    induty_code_to_sector("64202")  # → '금융 및 보험업'
    induty_code_to_sector(None)     # → None
"""
from __future__ import annotations

# KSIC 대분류 (중분류 2자리 코드 → 한글 대분류명).
# (lo, hi, name) 튜플. lo/hi 는 inclusive.
# 통계청 분류 기준이라 비연속 구간 존재 (예: 43, 53, 67, 69, 77~83, 88, 89, 92, 93 결번).
_SECTOR_RANGES: tuple[tuple[int, int, str], ...] = (
    (1, 3,   "농업, 임업 및 어업"),
    (5, 8,   "광업"),
    (10, 34, "제조업"),
    (35, 35, "전기·가스·증기 및 공기조절 공급업"),
    (36, 39, "수도·하수 및 폐기물 처리, 원료 재생업"),
    (41, 42, "건설업"),
    (45, 47, "도매 및 소매업"),
    (49, 52, "운수 및 창고업"),
    (55, 56, "숙박 및 음식점업"),
    (58, 63, "정보통신업"),
    (64, 66, "금융 및 보험업"),
    (68, 68, "부동산업"),
    (70, 73, "전문, 과학 및 기술 서비스업"),
    (74, 76, "사업시설 관리·사업 지원 및 임대 서비스업"),
    (84, 84, "공공행정, 국방 및 사회보장 행정"),
    (85, 85, "교육 서비스업"),
    (86, 87, "보건업 및 사회복지 서비스업"),
    (90, 91, "예술·스포츠 및 여가관련 서비스업"),
    (94, 96, "협회·단체, 수리 및 기타 개인 서비스업"),
    (97, 98, "가구 내 고용활동 등"),
    (99, 99, "국제 및 외국기관"),
)


def induty_code_to_sector(induty_code: str | None) -> str | None:
    """KSIC induty_code → 대분류 한글명.

    Args:
        induty_code: DART 가 주는 5자리 숫자 문자열. None / 빈 문자열 / 비숫자
                     는 None 반환. 4자리 이하 (zero-padded 안 됨) 도 처리.

    Returns:
        대분류명 (예: '제조업', '금융 및 보험업') 또는 None.
    """
    if not induty_code:
        return None
    s = str(induty_code).strip()
    if not s.isdigit():
        return None
    # KSIC 는 5자리지만 일부 응답에 leading-zero 가 빠져 있는 경우가 있음.
    # 길이 < 2 이면 대분류 추출 불가.
    if len(s) < 2:
        return None
    big = int(s[:2])
    for lo, hi, name in _SECTOR_RANGES:
        if lo <= big <= hi:
            return name
    return None


# 시장 구분 매핑 — DART corp_cls 1자리 → 한글 명칭
_CORP_CLS_NAME: dict[str, str] = {
    "Y": "KOSPI",
    "K": "KOSDAQ",
    "N": "KONEX",
    "E": "기타",
}


def corp_cls_to_market(corp_cls: str | None) -> str | None:
    """DART corp_cls → 사람이 읽는 시장명.

    Args:
        corp_cls: 'Y'/'K'/'N'/'E'. None / 미지원 값 → None.
    """
    if not corp_cls:
        return None
    return _CORP_CLS_NAME.get(corp_cls.strip().upper())


__all__ = [
    "induty_code_to_sector",
    "corp_cls_to_market",
]
