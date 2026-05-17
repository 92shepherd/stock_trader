"""FastAPI application factory with lifespan-managed scheduler.

Wiring summary:
    .env is loaded BEFORE any `src.*` import that touches credentials
    (KIS_*, DART_API_KEY, DB_*). This module is the API entry point,
    so we do it once here and rely on transitive imports thereafter.

    The lifespan context manager owns the APScheduler instance:
      - startup: build + register default jobs + start
      - shutdown: graceful stop (does not wait for in-flight jobs)

    Routers are mounted with their own prefixes; auth is applied at the
    router level (not globally) because /health needs to stay open.

Running:
    python -m src.api                # canonical entry, see __main__.py
    uvicorn src.api.app:app --reload # dev with auto-reload
"""
from __future__ import annotations

# .env first \u2014 BEFORE any src.* import that may read env vars at module
# scope (DB_*, KIS_*, DART_API_KEY, STOCK_TRADER_API_KEY).
#
# override=True 은 의도적: 세션에 이전에 설정된 (혹은 빈 값으로 남은) 동일한
# 환경변수가 있을 때에도 .env 파일을 권위 있는 소스로 삼는다. 이렇게
# 하지 않으면 예: PowerShell 세션에서 FNGUIDE_CONSENT_ACK=""가 먼저 설정된
# 경우 .env 의 =1 이 묵인다.
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)

from contextlib import asynccontextmanager  # noqa: E402
from typing import AsyncIterator  # noqa: E402

import asyncio  # noqa: E402

import psycopg  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from src.api.routers import collect as collect_router  # noqa: E402
from src.api.routers import health as health_router  # noqa: E402
from src.api.routers import jobs as jobs_router  # noqa: E402
from src.api.routers import research as research_router  # noqa: E402
from src.api.routers import schedule as schedule_router  # noqa: E402
from src.api.scheduler import start_scheduler, stop_scheduler  # noqa: E402
from src.config import get_db_settings  # noqa: E402
from src.db.migrate import run_migrations  # noqa: E402
from src.utils.logger import logger  # noqa: E402


# Advisory-lock key used by lifespan to serialize concurrent migration
# attempts when multiple API instances start simultaneously (e.g. a
# systemd restart loop, or two devs running `python -m src.api` against
# the same dev DB). The key is project-scoped under the same magic
# prefix as the collector locks but with a distinct low half so there's
# no collision risk. Held only across the run_migrations() call.
_MIGRATION_LOCK_KEY = 0x5754_0000_4D49_4752  # ASCII "ST\0\0" + "MIGR"
# Signed-int64 cast (Postgres pg_try_advisory_lock takes a signed bigint).
if _MIGRATION_LOCK_KEY >= 2 ** 63:
    _MIGRATION_LOCK_KEY -= 2 ** 64


def _run_migrations_with_lock() -> None:
    """Acquire a cluster-wide advisory lock and run migrations.

    Idempotent: `migrate.run_migrations` skips files already recorded in
    `schema_migrations`. The lock is purely there to prevent two starting
    processes from racing and producing confusing error messages — the
    `schema_migrations` PK would catch a true conflict, but seeing a
    clean "another process is migrating, waiting" path is friendlier.

    BLOCKING acquire (pg_advisory_lock, not pg_try_advisory_lock) so that
    a second starter waits politely instead of failing startup outright.
    """
    dsn = get_db_settings().psycopg_dsn
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,)
            )
        try:
            run_migrations()
        finally:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,)
                )


_APP_TITLE = "stock_trader API"
_APP_DESCRIPTION = (
    "REST API for triggering data collectors and managing the daily "
    "backfill schedule. Pairs with an in-process APScheduler that runs "
    "the default 03:00 KST KIS+DART cron. Per-collector locks ensure "
    "manual triggers and scheduled runs never collide.\n\n"
    "All non-health endpoints require an `X-API-Key` header matching "
    "STOCK_TRADER_API_KEY in .env."
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup steps (in order), then scheduler start, then yield.

    1. Run pending DB migrations. Idempotent via `schema_migrations`.
       Failure here aborts startup — a partially-migrated DB is
       strictly worse than a process that refuses to start, because
       collectors would silently write against the wrong schema.

    2. Start the in-process APScheduler. The scheduler is parked on
       `app.state.scheduler` so route handlers (e.g. /schedule) can
       inspect and control it. None when disabled via SCHEDULER_ENABLED.

    On shutdown: scheduler.shutdown(wait=False). In-flight collector
    jobs continue (they live in worker threads); the scheduler just
    stops dispatching new fires.
    """
    logger.info("[api] starting up")

    # --- Step 1: migrations ---
    # run_migrations() is blocking psycopg I/O. Offload to a thread so we
    # don't stall the event loop — even though startup is the only thing
    # happening right now, keeping the rule consistent (no blocking calls
    # on the loop) avoids future surprises.
    try:
        logger.info("[api] applying DB migrations (if any)")
        await asyncio.to_thread(_run_migrations_with_lock)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[api] migration step failed — aborting startup: {e}")
        raise

    # --- Step 2: scheduler ---
    scheduler = start_scheduler()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        logger.info("[api] shutting down")
        stop_scheduler(scheduler)
        app.state.scheduler = None


def create_app() -> FastAPI:
    """Factory used by `python -m src.api` and `uvicorn src.api.app:app`."""
    app = FastAPI(
        title=_APP_TITLE,
        description=_APP_DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
        # Auto-redirect of trailing slashes is fine; default behavior.
    )
    app.include_router(health_router.router)
    app.include_router(jobs_router.router)
    app.include_router(schedule_router.router)
    app.include_router(collect_router.router)
    app.include_router(research_router.router)
    return app


# Module-level instance for uvicorn's "module:attr" pattern.
app = create_app()
