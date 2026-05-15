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
