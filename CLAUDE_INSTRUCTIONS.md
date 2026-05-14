# Claude Desktop Project Instructions — `stock_trader`

> Claude Desktop 프로젝트 instruction. 모든 작업은 아래 규칙을 따른다.

---

## 1. Tooling — 필수

### 1.1 Filesystem MCP
모든 소스 코드 작성/수정은 `filesystem` MCP로 직접 수행.

- 프로젝트 루트: `C:\Users\Playdata\workspace\stock_trader`
- 경로는 **항상 Windows 절대경로**
- 구조 파악: `directory_tree` (결과는 `result[0]['text']` unwrap) + `read_multiple_files` 조합
- 파일 작성: `write_file` / 부분 수정: `edit_file` (character-exact 매칭)
- ❌ 채팅창에 코드만 출력하고 "복사하세요" 하지 않기
- ❌ `search_files`로 내용 검색 시도 (파일명만 검색됨)
- ❌ 삭제 기능 없음 — 필요 시 사용자에게 수동 요청

### 1.2 Context7 MCP
외부 라이브러리/API 코드 작성 전에 **반드시** Context7로 최신 문서 조회.

- `resolve-library-id` → `query-docs`
- ❌ memory나 가정만으로 API 시그니처 작성하지 않기

---

## 2. 프로젝트 목표

1. **주가 정보 수집 — KIS API**: 일봉/분봉/현재가/호가, access_token 캐싱, TR_ID 정확히 매칭, rate limit 준수
2. **공시지표 수집 — DART API**: 정기보고서/주요사항/재무제표(XBRL), `corp_code.xml` 마스터 동기화
3. **종목 컨센서스 수집**: 애널리스트 예상치(EPS/매출/영업이익/목표주가) 시계열 추적
4. **증권사 투자의견 수집**: 리포트의 의견(Buy/Hold/Sell), 목표주가, 발행일/애널리스트 메타데이터

---

## 3. 코딩 컨벤션

- 모든 모듈 상단에 `from __future__ import annotations`
- Type hints 필수
- HTTP 클라이언트는 **`httpx`** (❌ `requests`)
- 환경변수는 `.env` + `load_dotenv()`를 파이프라인 진입점 최상단에서 호출
- `pyproject.toml` 프로젝트명은 `stock-trader`

### Collector 패턴 (필수 요소)
- `COLLECTOR_NAME` 상수
- 외부 호출은 `_safe_` 접두어 + Tenacity `@retry` (지수 백오프)
- 방어적 응답 스키마 검증
- CLI 플래그: `--start-date`, `--end-date`, `--symbols`, `--skip-done`, `--dry-run`
- `skip_done=True` 기본 + 연속 실패 circuit breaker

### Secrets
- API 키는 **반드시** `.env` (`.env.example`엔 키 이름만)
- 로그에서 토큰/키 마스킹
- 예시 키:
  ```
  KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO
  KIS_BASE_URL (실전), KIS_PAPER_URL (모의)
  DART_API_KEY
  ```

---

## 4. API별 주의점

- **KIS**: access_token 24h 캐시(매 호출 신규 발급 금지), 실전/모의 TR_ID 다름, 분봉은 당일 위주
- **DART**: `corp_code.xml`은 ZIP → 해제 후 마스터 테이블화, 분당 호출 한도 → throttle
- **스크래핑(컨센서스/투자의견)**: `robots.txt` 및 약관 준수, User-Agent 명시, selector 상수화 + 변경 감지

---

## 5. 워크플로우

요구사항 확인 → Context7 문서 조회 → `directory_tree` + `read_multiple_files`로 기존 코드 파악 → (필요 시) `migrations/NNN_*.sql` (번호 충돌 금지) → collector/client 작성 → CLI 스크립트 → 테스트 → 변경 파일 경로 + 핵심 변경점 요약 보고.

긴 코드는 채팅에 다시 출력하지 않음. 모호한 요구사항은 작업 시작 전에 묶어서 질문.

---

## 6. 핵심 금지사항

❌ `requests` 사용 / API 키 하드코딩 / 매직 넘버 / 매 호출 토큰 신규 발급 / row-by-row INSERT / 재시도 없는 외부 API / 가정 기반 API 작성 / 마이그레이션 번호 중복 / 상대경로
