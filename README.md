# Stock Data Pipeline (KOSPI + KOSDAQ)

KOSPI + KOSDAQ 전 종목의 일봉/분봉을 TimescaleDB에 수집·저장하는 파이프라인.

## 스택

- **DB**: TimescaleDB (PostgreSQL 16) — Docker
- **언어**: Python 3.11+
- **데이터 소스**: pykrx (일봉), KIS OpenAPI (분봉, 계좌 개설 후)
- **DB 접근**: SQLAlchemy 2.0 + psycopg3 (COPY 벌크 INSERT)
- **패키지 관리**: uv

## 디렉토리 구조

```
stock-data/
├── docker-compose.yml         # TimescaleDB 컨테이너
├── pyproject.toml             # 의존성 정의
├── .env.example               # 환경변수 템플릿
├── config/settings.yaml       # 수집 설정
├── migrations/                # SQL 스키마
│   ├── 001_init_schema.sql
│   ├── 002_hypertables.sql
│   ├── 003_compression.sql
│   └── 004_continuous_aggregates.sql
├── src/
│   ├── config.py              # 설정 로더
│   ├── db/                    # DB 레이어
│   │   ├── connection.py
│   │   ├── models.py
│   │   ├── migrate.py
│   │   └── repositories.py
│   ├── collectors/            # 데이터 수집기
│   │   ├── tickers.py
│   │   └── daily_pykrx.py
│   ├── pipelines/             # 실행 엔트리포인트
│   │   └── collect_daily.py
│   └── utils/                 # 로거, 영업일
├── scripts/
│   ├── init_db.py             # 최초 DB 초기화
│   └── verify_data.py         # 수집 검증
└── data/
    ├── pgdata/                # TimescaleDB 볼륨 (gitignore)
    └── logs/                  # 로그 파일
```

## 초기 세팅 (최초 1회)

### 1. 환경 변수 준비
```bash
cp .env.example .env
# .env 열어서 DB_PASSWORD를 원하는 값으로 변경
```

### 2. Python 가상환경 + 의존성
```bash
# uv 추천 (없으면 설치: pip install uv)
uv venv
source .venv/bin/activate     # Linux/Mac
# .venv\Scripts\activate      # Windows
uv pip install -e .
```

### 3. TimescaleDB 실행
```bash
docker compose up -d
docker compose logs -f timescaledb     # 초기화 로그 확인, 준비되면 Ctrl+C
```

### 4. DB 스키마 생성
```bash
python -m scripts.init_db
```
→ migrations/ 안의 SQL 4개가 순서대로 적용됩니다.

## 일상 사용

### 일봉 수집 — 최초 백필 (1년치)
```bash
python -m src.pipelines.collect_daily --days 400
```
- KOSPI+KOSDAQ 전 종목
- 약 2~4시간 소요 (KRX 서버 부하 배려해 종목당 0.3초 딜레이)
- 중단 후 재실행하면 `collection_log`가 성공한 날짜는 건너뜀

### 일봉 수집 — 특정 구간
```bash
python -m src.pipelines.collect_daily --start 2024-04-21 --end 2025-04-21
```

### 일봉 수집 — 오늘치만 (크론용)
```bash
python -m src.pipelines.collect_daily --today --skip-tickers
```

### 데이터 검증
```bash
python -m scripts.verify_data
```
테이블 row 수, 날짜 범위, 삼성전자 최근 30일, 실패 로그 등을 출력합니다.

### 직접 SQL 쿼리
```bash
docker exec -it stock_timescaledb psql -U stock -d stockdata
```

샘플 쿼리:
```sql
-- 삼성전자 최근 20일
SELECT date, close, volume
FROM daily_prices
WHERE symbol = '005930'
ORDER BY date DESC LIMIT 20;

-- 특정 날짜 거래대금 상위 10종목
SELECT t.name, d.close, d.value
FROM daily_prices d JOIN tickers t USING (symbol)
WHERE d.date = '2025-04-18'
ORDER BY d.value DESC LIMIT 10;

-- PER 10 이하 + 배당수익률 3% 이상 (가치주 스크리닝)
SELECT t.name, d.close, d.per, d.dividend_yield
FROM daily_prices d JOIN tickers t USING (symbol)
WHERE d.date = (SELECT MAX(date) FROM daily_prices)
  AND d.per BETWEEN 0 AND 10
  AND d.dividend_yield >= 3.0
ORDER BY d.dividend_yield DESC;
```

## 자동화 (cron 예시)

매 거래일 16:30에 당일 데이터 수집:
```cron
30 16 * * 1-5 cd /path/to/stock-data && /path/to/.venv/bin/python -m src.pipelines.collect_daily --today --skip-tickers >> data/logs/cron.log 2>&1
```

매주 일요일 종목 마스터 리프레시:
```cron
0 9 * * 0 cd /path/to/stock-data && /path/to/.venv/bin/python -c "from src.collectors.tickers import collect_tickers; collect_tickers()" >> data/logs/cron.log 2>&1
```

## 다음 단계 (분봉)

현재 구조는 분봉 수집을 위해 이미 `minute_prices` 테이블과 5분봉/시간봉 Continuous Aggregates가 준비되어 있습니다. KIS 계좌 개설 후:

1. https://apiportal.koreainvestment.com 에서 App Key / Secret 발급
2. `.env`의 `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO` 채우기
3. `src/kis/` 아래 클라이언트 구현 → `src/collectors/minute_kis.py` 추가
4. 크론으로 매일 15:40 이후 당일 분봉 수집 (누적 방식)

## 트러블슈팅

**Q. `docker compose` 명령이 없다**
→ Docker Desktop 최신 버전 설치 (Docker Compose V2 내장)

**Q. pykrx에서 `JSONDecodeError`**
→ KRX 일시적 장애. 스크립트에 내장된 tenacity 재시도로 대부분 자동 복구됨

**Q. 백필이 너무 느리다**
→ `config/settings.yaml`의 `request_delay`를 0.1~0.2로 낮출 수 있으나, KRX 차단 가능성이 올라감. 0.3 권장

**Q. 디스크 공간 부족**
→ `data/pgdata` 위치 확인. 압축 정책이 3개월 후부터 적용되므로 초기엔 압축 전 크기 기준으로 계산 필요
