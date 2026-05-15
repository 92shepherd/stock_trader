# stock_trader REST API

FastAPI 기반 수집기 트리거 API + 내장 APScheduler. 단일 프로세스에서
스케줄러와 수동 API가 **per-collector 락**을 공유해 충돌을 원천 차단한다.

## 실행 — Docker Compose (권장)

기본 스택: TimescaleDB + API 서버 두 컨테이너. DB는 내부 네트워크에만 연결되고,
API만 호스트 `API_PORT`(기본 8765)로 노출된다.

```powershell
# 1) .env 준비 (.env.example 속에 있는 값들 채우기)
#    필수: DB_PASSWORD, STOCK_TRADER_API_KEY
#    권장: KIS_*, DART_API_KEY (비어 있으면 해당 collector만 실패)

# 2) 이미지 빌드 + 기동
docker compose up -d --build

# 3) 로그 확인 (마이그레이션 + 스케줄 등록 확인)
docker compose logs -f api

# 4) 헬스철크
docker compose ps
# api 서비스가 healthy 상태면 완료
```

재기동 / 업데이트:

```powershell
# 코드 변경 후 재빌드만 필요한 경우
docker compose build api && docker compose up -d api

# 설정만 바뀌면 (환경변수 등)
docker compose up -d api

# 전체 종료
docker compose down
```

### 개발 용 오버라이

`docker-compose.dev.yml`을 겹쳐서 쓰면:
- TimescaleDB 포트가 호스트 `127.0.0.1:5432`로 노출 (psql / DBeaver 접속 가능)
- 소스 읽기 전용으로 컨테이너에 바인드되어 `restart` 만으로 수정사항 반영

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# 소스 수정 후
docker compose restart api
```

## 실행 — 호스트 직접 (보조)

Docker를 쓰고 싶지 않거나 개발 중 일회성 테스트 용:

```powershell
# DB는 컴포즈로 띄우고 (개발 오버라이와 함께 띄워야 호스트에서 볼 수 있음)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d timescaledb

# API만 호스트에서 직접 실행
python -m src.api
```

서버 시작 시 lifespan 첫 단계에서 `migrations/*.sql`을 자동 적용하고 스케줄러가 기동한다.
수동 마이그레이션이 필요하면 `python -m scripts.init_db`도 그대로 쓸 수 있다.

기존 Windows 작업 스케줄러의 `python -m src.main` 잡은 API 가동 후 제거.
APScheduler가 같은 일을 더 안전하게 처리한다.

## 인증

모든 비-health 엔드포인트는 `X-API-Key` 헤더 필수.

```bash
curl -H "X-API-Key: $STOCK_TRADER_API_KEY" http://127.0.0.1:8765/schedule
```

키 미설정 → 401 (조용한 통과 없음). 키 불일치 → 401.

## 엔드포인트 요약

| Method | Path                      | 동작         | 응답              |
|--------|---------------------------|--------------|-------------------|
| GET    | `/health`                 | 라이브니스 | 200 (인증 불요)   |
| GET    | `/health/full`            | DB+스케줄러 | `HealthResponse`  |
| GET    | `/jobs`                   | 잡 목록      | `JobListResponse` |
| GET    | `/jobs/{id}`              | 잡 상세      | `JobStatusResponse` |
| GET    | `/schedule`               | 스케줄 목록  | `ScheduleListResponse` |
| POST   | `/schedule/{id}/pause`    | 일시정지     | `SchedulePauseResponse` |
| POST   | `/schedule/{id}/resume`   | 재개         | `SchedulePauseResponse` |
| POST   | `/collect/tickers/kr`     | KR 마스터    | **동기** `SyncRunResponse` |
| POST   | `/collect/tickers/us`     | US 마스터    | **동기** `SyncRunResponse` |
| POST   | `/collect/dart/corp-codes`| corp_codes   | **동기** `SyncRunResponse` |
| POST   | `/collect/daily/fdr`      | KR 일봉(FDR) | **비동기** 202 `JobAcceptedResponse` |
| POST   | `/collect/daily/kis`      | KR 일봉(KIS) | **비동기** 202 |
| POST   | `/collect/daily/us`       | US 일봉(yf)  | **비동기** 202 |
| POST   | `/collect/dart/disclosures`| 공시 목록   | **비동기** 202 |
| POST   | `/collect/dart/financials`| 재무제표     | **비동기** 202 |
| POST   | `/collect/dart/indicators`| 재무지표     | **비동기** 202 |
| POST   | `/collect/daily-cron`     | KIS+DART 묶음 | **비동기** 202 (스케줄 cron 수동 실행) |

OpenAPI 스키마: `http://127.0.0.1:8765/docs` (Swagger UI).

## 동기 vs 비동기

- **동기**: 짧은 작업 (수 초~수십 초). HTTP 응답이 끝날 때까지 차단.
  - 이미 같은 collector가 돌면 `409 Conflict`.
- **비동기**: 긴 작업 (수 분 ~ 수 시간). 즉시 `job_id` 반환.
  - `GET /jobs/{id}`로 상태 확인 (`pending` → `running` → `success`/`failed`/`rejected`).
  - 같은 collector가 이미 돌고 있으면 잡이 등록은 되지만 상태가 `rejected`로 마감된다.

## 사용 예시

```bash
# 어제 데이터 강제 재수집 (KIS+DART)
curl -X POST -H "X-API-Key: $KEY" \
     -H "Content-Type: application/json" \
     -d '{"days": 1, "skip_done": false}' \
     http://127.0.0.1:8765/collect/daily-cron
# → {"job_id": "abc123...", "collector": "daily_cron", "status": "pending", ...}

# 상태 확인
curl -H "X-API-Key: $KEY" http://127.0.0.1:8765/jobs/abc123...

# 특정 종목만 KIS 일봉 백필
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"symbols": ["005930", "000660"], "days": 30}' \
     http://127.0.0.1:8765/collect/daily/kis

# 일일 cron 일시정지 (장기 백필 중 충돌 회피)
curl -X POST -H "X-API-Key: $KEY" \
     http://127.0.0.1:8765/schedule/daily_kis_dart_cron/pause
```

## 충돌 방지 — 3중 락 동작 방식

1. **`asyncio.Lock`** (collector별, 프로세스 내)
   - API ↔ Scheduler 충돌 차단의 1차 방어선
   - 비차단: 락이 잡혀 있으면 즉시 `CollectorBusy`
2. **PostgreSQL advisory lock** (collector별, 클러스터 전체)
   - 같은 DB를 보는 다른 OS 프로세스(구 cron, 수동 `python -m src.pipelines.*`)도 감지
   - `pg_try_advisory_lock(키)`, 키는 collector 이름 sha256 기반
3. **APScheduler `max_instances=1` + `coalesce=True`**
   - 스케줄러 자체가 동일 잡의 중복 실행을 차단
   - 다운타임 후 회복 시 누적된 fire를 1번으로 합침

API 응답 매핑:
- 동기 라우트 + 락 보유 중 → `HTTP 409 Conflict`
- 비동기 라우트 + 락 보유 중 → 202 응답 후 잡 `rejected`
- 스케줄 잡 + 락 보유 중 → 로그만 남기고 다음 fire까지 스킵

## 멀티프로세스/멀티워커 금지

`__main__.py`는 `workers=1`로 고정. 워커를 늘리면 in-process 락이 무효화돼
같은 collector가 N번 동시에 돌 수 있다. 부하가 늘면 워커가 아닌 수직 스케일
(스케줄러를 다른 호스트로 옮기고 API만 ReadOnly 모드로 분리) 방식으로 풀어야 한다.
