"""Health endpoints.

`GET /health` is intentionally not behind the API key so a load balancer
or local monitoring script can poll it without secrets. It does NOT
leak per-collector or DB state details beyond what a basic liveness
probe needs.

`GET /health/full` IS behind the API key and exposes which collectors
are busy and whether the scheduler is alive \u2014 useful for ops dashboards
but not for unauthenticated probes.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text as sql_text

from src.api.auth import require_api_key
from src.api.locks import busy_collectors
from src.api.schemas import HealthResponse
from src.db.connection import session_scope
from src.utils.logger import logger

router = APIRouter(tags=["health"])


def _db_ok() -> bool:
    """Cheap connectivity probe \u2014 a single SELECT 1."""
    try:
        with session_scope() as session:
            session.execute(sql_text("SELECT 1")).scalar_one()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"health: DB probe failed: {e}")
        return False


@router.get("/health", summary="Liveness probe (no auth)")
async def health() -> dict[str, str]:
    """Always returns ok=200 if the process is up.

    Intentionally trivial: we don't check the DB here because a flaky
    DB shouldn't take down the process from a load balancer's POV.
    Use /health/full for deep checks.
    """
    return {"status": "ok"}


@router.get(
    "/health/full",
    response_model=HealthResponse,
    summary="Deep health (auth required)",
    dependencies=[Depends(require_api_key)],
)
async def health_full(request: Request) -> HealthResponse:
    """Reports which collectors are busy, scheduler state, and DB ping."""
    busy = [c.value for c in busy_collectors()]
    scheduler = getattr(request.app.state, "scheduler", None)
    scheduler_running = bool(scheduler and scheduler.running)
    db_ok = _db_ok()
    overall = "ok" if db_ok else "degraded"
    return HealthResponse(
        status=overall,
        busy_collectors=busy,
        scheduler_running=scheduler_running,
        db_ok=db_ok,
    )


@router.get(
    "/health/debug-env",
    summary="TEMP: env var visibility check (auth required, remove after debug)",
    dependencies=[Depends(require_api_key)],
)
async def debug_env() -> dict:
    """임시 진단 엔드포인트.

    API 프로세스가 실제로 보는 환경변수와 pydantic-settings 결과를
    비교해서 FnGuide consent 안 읽히는 원인 추적. 테스트 끝나면 제거.
    시크릿 자체는 노출하지 않고 "존재 여부 + 길이"만 표시.
    """
    import os
    from pathlib import Path

    from src.config import (
        PROJECT_ROOT,
        get_db_settings,
        get_fnguide_settings,
        get_kis_settings,
    )

    def _safe(key: str) -> dict:
        v = os.environ.get(key)
        if v is None:
            return {"present": False, "length": 0}
        return {"present": True, "length": len(v)}

    # .env file diagnostics
    cwd = Path.cwd()
    env_in_cwd = cwd / ".env"
    env_in_root = PROJECT_ROOT / ".env"

    # dotenv direct parse vs pydantic-settings result
    try:
        from dotenv import dotenv_values
        parsed = dotenv_values(env_in_root)
        fnguide_in_parsed = parsed.get("FNGUIDE_CONSENT_ACK")
        all_keys_count = len(parsed)
    except Exception as e:
        fnguide_in_parsed = f"ERROR: {e}"
        all_keys_count = -1

    fnguide_settings = get_fnguide_settings()

    return {
        "cwd": str(cwd),
        "project_root": str(PROJECT_ROOT),
        "env_in_cwd_exists": env_in_cwd.exists(),
        "env_in_root_exists": env_in_root.exists(),
        "env_paths_match": str(env_in_cwd) == str(env_in_root),
        "dotenv_direct_parse": {
            "FNGUIDE_CONSENT_ACK": repr(fnguide_in_parsed),
            "total_keys": all_keys_count,
        },
        "os_environ": {
            "FNGUIDE_CONSENT_ACK": _safe("FNGUIDE_CONSENT_ACK"),
            "DB_HOST": _safe("DB_HOST"),
            "DART_API_KEY": _safe("DART_API_KEY"),
            "KIS_APP_KEY": _safe("KIS_APP_KEY"),
            "KIS_APP_SECRET": _safe("KIS_APP_SECRET"),
            "STOCK_TRADER_API_KEY": _safe("STOCK_TRADER_API_KEY"),
        },
        "pydantic_settings": {
            "FnguideSettings.fnguide_consent_ack": repr(
                fnguide_settings.fnguide_consent_ack
            ),
            "FnguideSettings.consent_ack": fnguide_settings.consent_ack,
            "DBSettings.db_host": get_db_settings().db_host,
            "KISSettings.kis_app_key_len": len(get_kis_settings().kis_app_key),
        },
    }
