# syntax=docker/dockerfile:1.7
#
# stock_trader API 이미지 — 멀티스테이지 빌드.
#
# 설계:
#   1. builder 스테이지에서 wheel 캐시 + 의존성 설치 (build-essential 포함).
#   2. runtime 스테이지에는 site-packages만 복사 (빌드 도구 없음).
#   3. 비-root user(`app`)로 실행 → 컨테이너 탈출 위험 최소화.
#   4. data/ 와 logs/는 외부 볼륨에서 마운트 → 컨테이너 재생성 시에도 토큰 캐시,
#      DART corp_codes 캐시, 로그가 보존.
#
# 베이스: python:3.11-slim (psycopg[binary]가 매뉴얼 빌드를 안 해서 충분).
# 빌드 명령: docker compose build api
# 실행 명령: docker compose up -d
#
# 캠시 전략 참고: pyproject.toml의 [tool.setuptools].packages 명세가 구체적인
# 디렉터리 목록을 요구해서, deps만 먼저 깔려는 전형적인 "pyproject 먼저
# COPY" 캐시 레이어 전략은 동작하지 않는다. 대신 pip의 로컬 wheel
# 캐시에 의존해 소스 변경 시 네트워크 호출 없이 재설치한다.

# =====================================================================
# Stage 1: builder
# =====================================================================
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# pip의 wheel 캠시를 강제하지 않아서 (PIP_NO_CACHE_DIR 미설정),
# 소스만 바뀜 재빌드 시 이미 다운로드한 wheel을 재사용한다.
# (몀티스테이지 빌드의 builder 레이어 캐시 + pip wheel 캐시 이중으로
# 재빌드가 빠르다.)

# build-essential: 일부 의존성(예: yfinance가 끌어오는 lxml의 일부 경로,
# pandas/scipy의 빌드 폴백)에서 필요할 수 있음. tini는 runtime 스테이지로 미룬다.
# curl은 진단용 — slim 이미지엔 없으므로 추가.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# pip만 먼저 업그레이드. 패키지 메타데이터가 바뀌더라도 이 레이어는 재사용된다.
RUN pip install --upgrade pip

# 소스 전체를 복사한 뒤 한 번에 설치.
# 캠시 전략으로 pyproject.toml만 먼저 복사해 deps만 설치해두는 패턴이
# 일반적이지만, 이 프로젝트의 setuptools 설정이 구체적인 패키지
# 디렉터리 존재를 요구하므로 (`packages = ["src.api", ...]`), 더미
# src/ 레이아웃만으로는 설치가 실패한다. 패키지 목록을 추가할 때마다
# Dockerfile에 더미 디렉터리를 반영하는 건 부서져서, 소스 복사를
# 먼저 하고 한 번에 설치하는 단순 경로로 간다. pip이 구문 의존성
# wheel을 로컬 캐시에 계속 두고 쓰기 때문에, 소스만 바뀐 재빌드도
# 네트워크 호출 없이 수십 초에 끝난다.
COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations
COPY config ./config

RUN pip install .


# =====================================================================
# Stage 2: runtime
# =====================================================================
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Seoul

# tini: PID 1로 동작해 SIGTERM을 uvicorn에 정확히 전달 → graceful shutdown.
#       기본 python 프로세스는 PID 1일 때 시그널 핸들러가 제대로 안 동작한다.
# tzdata: 컨테이너 시계를 KST로 맞춰야 APScheduler의 "Asia/Seoul" 트리거가
#         호스트 시각과 일치한다 (Asia/Seoul tz 자체는 zoneinfo로 들어옴).
# curl: HEALTHCHECK 명령어용. /health 엔드포인트를 찌른다.
# ca-certificates: HTTPS(KIS, DART, Yahoo) 호출에 필요.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        tini \
        tzdata \
        curl \
        ca-certificates \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
 && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

# 비-root user. uid/gid는 호스트에서 마운트되는 ./data 디렉터리 소유권과
# 충돌하지 않도록 1000 (대부분의 리눅스 환경 첫 사용자) 사용. Windows에서
# Docker Desktop을 쓴다면 호스트 UID는 무의미 (VM 안에서 동작).
RUN groupadd -r app --gid 1000 \
 && useradd -r -g app --uid 1000 --home-dir /app --shell /sbin/nologin app

# builder 스테이지의 site-packages와 console scripts를 복사.
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 소스도 복사. `pip install .`로 패키지가 site-packages에도 들어있지만,
# 컨테이너에서 운영 도중 로그를 보거나 SQL 파일을 확인할 때를 위해
# 동일한 디렉터리 구조를 유지한다.
WORKDIR /app
COPY --from=builder /build/src ./src
COPY --from=builder /build/migrations ./migrations
COPY --from=builder /build/config ./config

# 마운트 대상 디렉터리 사전 생성 + 소유권. 비-root로 실행하므로 권한 필수.
RUN mkdir -p /app/data/logs /app/data/kis /app/docs_cache \
 && chown -R app:app /app

USER app

# 컨테이너 내부 기본 바인딩: 0.0.0.0 (compose가 호스트로 publish해야 외부 접근).
# 외부에서 127.0.0.1로만 보고 싶으면 compose에서 "127.0.0.1:8765:8765" 형태로 publish.
ENV API_HOST=0.0.0.0 \
    API_PORT=8765 \
    LOG_DIR=/app/data/logs

EXPOSE 8765

# 컨테이너 헬스체크 — 인증 없이 통과되는 /health에 의존.
# start_period 60s: 마이그레이션이 처음 적용될 때를 감안한 여유.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8765/health || exit 1

# tini로 wrap → uvicorn이 PID 1이 아니어도 시그널 전달 정상.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "src.api"]
