"""In-process APScheduler that runs the default daily backfill cron.

What this replaces:
    `python -m src.main`, previously registered in Windows Task
    Scheduler at 03:00 KST. We bring that schedule INSIDE the FastAPI
    process so it shares per-collector locks with manual API triggers.
    The old Windows task can be removed once this is rolled out.

How it integrates with the locks layer:
    The scheduled job dispatches through `runners.submit_daily_cron`,
    which acquires `CollectorName.DAILY_CRON`. The composite now bundles
    KIS daily prices + DART corp_codes/disclosures + FnGuide consensus.
    The FnGuide step auto-skips when `FNGUIDE_CONSENT_ACK` is not set
    to 1 in .env (silent skip, not a failure).

    Any manual API trigger for the composite OR any inner collector
    (daily_kis, dart_*, consensus_fnguide) gets rejected with
    `CollectorBusy` while DAILY_CRON is held.

What this is NOT:
    - It is not a generic task queue. Async API submissions go through
      JobRegistry, not through here.
    - It is not persistent. Restart the process and missed runs are
      lost; APScheduler's misfire policy decides whether the next run
      catches up. We use `coalesce=True` so a 6-hour outage doesn't
      cause 6 piled-up runs the moment we come back \u2014 just one.

Cron expression:
    Read from .env (SCHEDULER_DAILY_CRON, default '0 3 * * *'). Parsed
    via APScheduler's `CronTrigger.from_crontab` so it accepts standard
    5-field crontab syntax (MIN HOUR DAY MONTH DOW).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.api.runners import (
    submit_consensus_hankyung,
    submit_daily_cron,
    submit_factor_eval_all,
)
from src.utils.logger import logger


# Job IDs (stable strings so /schedule endpoints can address them).
JOB_ID_DAILY_CRON = "daily_kis_dart_cron"
JOB_ID_HANKYUNG_CRON = "daily_hankyung_cron"
JOB_ID_FACTOR_EVAL_CRON = "weekly_factor_eval_cron"
JOB_ID_MINUTE_REALTIME = "minute_realtime_cron"


# ---------------------------------------------------------------------------
# Env helpers \u2014 read at startup, never re-read (matches main.py contract)
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _scheduler_enabled() -> bool:
    return _env_bool("SCHEDULER_ENABLED", True)


def _daily_cron_expression() -> str:
    return os.getenv("SCHEDULER_DAILY_CRON", "0 3 * * *").strip() or "0 3 * * *"


def _timezone_name() -> str:
    return os.getenv("SCHEDULER_TIMEZONE", "Asia/Seoul").strip() or "Asia/Seoul"


def _hankyung_enabled() -> bool:
    """HANKYUNG_ENABLED 환경변수로 한경 컨센서스 스케줄 잡 활성화 여부 결정.

    기본값 False — docker-compose.gcp-db.yml 에서만 'true' 로 설정됨.
    """
    return _env_bool("HANKYUNG_ENABLED", False)


def _factor_eval_enabled() -> bool:
    """FACTOR_EVAL_ENABLED 환경변수로 팩터 평가 스케줄 잡 활성화 여부 결정.

    기본값 False. 명시적으로 true 로 설정해야 활성화됨.
    """
    return _env_bool("FACTOR_EVAL_ENABLED", False)


def _factor_eval_cron_expression() -> str:
    """SCHEDULER_FACTOR_EVAL_CRON 환경변수. 기본: 매주 일요일 04:00 KST."""
    return (
        os.getenv("SCHEDULER_FACTOR_EVAL_CRON", "0 4 * * 0").strip()
        or "0 4 * * 0"
    )


def _factor_eval_horizon_days() -> int:
    """FACTOR_EVAL_HORIZON_DAYS 환경변수. 기본 5일."""
    raw = os.getenv("FACTOR_EVAL_HORIZON_DAYS", "5").strip()
    try:
        return int(raw)
    except ValueError:
        return 5


def _minute_realtime_enabled() -> bool:
    """MINUTE_REALTIME_ENABLED 환경변수. 기본 False.

    docker-compose.yml (로컬/일반 모드) 에서만 활성화.
    docker-compose.gcp-db.yml 에는 설정하지 않아 비활성 유지.
    """
    return _env_bool("MINUTE_REALTIME_ENABLED", False)


def _minute_realtime_symbols() -> list[str] | None:
    """MINUTE_REALTIME_SYMBOLS: 쉼표 구분 종목코드 목록.

    빈값이면 None 반환 → 잡에서 전체 활성 종목 사용.
    예: MINUTE_REALTIME_SYMBOLS=005930,000660,035720
    """
    raw = os.getenv("MINUTE_REALTIME_SYMBOLS", "").strip()
    if not raw:
        return None
    return [s.strip().zfill(6) for s in raw.split(",") if s.strip()]


def _daily_cron_only() -> str | None:
    """Narrow the composite cron to a single step via SCHEDULER_DAILY_CRON_ONLY.

    Accepted values: 'kis', 'dart', 'fnguide'. Unset or empty → all steps run.
    Invalid values are logged and ignored (all steps run).
    """
    raw = os.getenv("SCHEDULER_DAILY_CRON_ONLY", "").strip().lower()
    if not raw:
        return None
    valid = ("kis", "dart", "fnguide")
    if raw not in valid:
        logger.warning(
            f"[scheduler] SCHEDULER_DAILY_CRON_ONLY={raw!r} is not one of "
            f"{valid} — ignoring, all steps will run"
        )
        return None
    return raw


# ---------------------------------------------------------------------------
# Scheduled job bodies
# ---------------------------------------------------------------------------


async def _scheduled_daily_cron() -> None:
    """Body of the 03:00 KST job. Composite KIS + DART + FnGuide.

    Defaults: end_date=yesterday, days=1, snapshot on, skip_done on.
    SCHEDULER_DAILY_CRON_ONLY narrows to a single step when set.
    The FnGuide step itself self-disables without FNGUIDE_CONSENT_ACK=1,
    so this job is safe to run on installs that haven't opted into FnGuide.

    Submits through `runners.submit_daily_cron`, which means:
      - A JobRecord is created so /jobs/{id} can show progress.
      - The DAILY_CRON lock is acquired (or the run is rejected if a
        manual trigger is already holding it).
      - Failures are captured in the record's `error` field AND in the
        normal loguru logs.
    """
    only = _daily_cron_only()
    label = only or "KIS+DART+FnGuide"
    logger.info(f"[scheduler] firing daily composite cron ({label})")
    try:
        record = await submit_daily_cron(only=only)
        logger.info(
            f"[scheduler] submitted job_id={record.id} for daily cron"
        )
    except Exception as e:  # noqa: BLE001
        # `submit_daily_cron` shouldn't raise (it returns a JobRecord
        # marked rejected/failed instead) but guard anyway: a scheduler
        # crash terminates *all* future runs, so we eat the exception.
        logger.exception(f"[scheduler] daily cron submission failed: {e}")


async def _scheduled_factor_eval_cron() -> None:
    """팩터 평가 주간 스케줄 잡 — 전체 팩터를 순서대로 평가.

    FACTOR_EVAL_ENABLED=true 일 때만 register_default_jobs() 에서 등록됨.
    기간: 오늘 기준 1년 전 ~ 어제 (매주 갱신).
    horizon_days: FACTOR_EVAL_HORIZON_DAYS (기본 5).
    persist_signals=False (디스크 절약), persist_run=True.
    """
    logger.info("[scheduler] firing weekly factor eval cron")
    try:
        record = await submit_factor_eval_all(
            horizon_days=_factor_eval_horizon_days(),
            persist_signals=False,
            persist_run=True,
        )
        logger.info(
            f"[scheduler] submitted job_id={record.id} for factor eval cron"
        )
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] factor eval cron submission failed: {e}")


async def _scheduled_hankyung_cron() -> None:
    """한경 컨센서스 일 스케줄 잡 — 전일 리포트 수집.

    HANKYUNG_ENABLED=true 일 때만 register_default_jobs() 에서 등록됨.
    전일(yesterday) 을 target_date 로 고정하여 수집. skip_done=True 이므로
    같은 날 수동 트리거가 이미 실행된 경우 0건으로 즉시 완료.
    """
    from datetime import date, timedelta
    yesterday = date.today() - timedelta(days=1)
    logger.info(f"[scheduler] firing hankyung cron for {yesterday}")
    try:
        record = await submit_consensus_hankyung(
            target_date=yesterday,
            skip_done=True,
        )
        logger.info(
            f"[scheduler] submitted job_id={record.id} for hankyung cron "
            f"(target_date={yesterday})"
        )
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] hankyung cron submission failed: {e}")


async def _scheduled_minute_realtime() -> None:
    """매분 실행 — 장중 최신 1분봉 수집.

    MINUTE_REALTIME_ENABLED=true 일 때만 register_default_jobs() 에서 등록됨.
    장외 시간(주말 포함 09:00~15:35 KST 이외)에는 collect_latest_bars 내부에서
    즉시 스킵하므로 스케줄러 자체는 항상 매분 발화해도 무방.

    MINUTE_REALTIME_SYMBOLS 가 설정된 경우 해당 종목만, 미설정 시 전체 활성 종목.
    수집 결과는 DEBUG 레벨로만 기록 (분당 로그 범람 방지).
    """
    from src.collectors.minute_kis import collect_latest_bars
    from src.config import get_app_config
    from src.db.repositories import get_active_tickers

    symbols = _minute_realtime_symbols()
    if symbols is None:
        cfg = get_app_config()
        tickers = get_active_tickers(markets=cfg.markets)
        symbols = [t.symbol for t in tickers]

    if not symbols:
        return

    try:
        result = await asyncio.to_thread(collect_latest_bars, symbols)
        if result.get("skipped_market_closed"):
            logger.info(
                f"[minute_realtime] skipped — market closed "
                f"(symbols={result['n_symbols']})"
            )
        else:
            logger.info(
                f"[minute_realtime] rows={result['total_rows']} "
                f"ok={result['n_ok']} failed={result['n_failed']} "
                f"symbols={result['n_symbols']}"
            )
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[minute_realtime] job error: {e}")


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------


def build_scheduler() -> AsyncIOScheduler:
    """Construct the scheduler with sensible defaults for this app.

    `coalesce=True`: if the process was down across N missed fire times,
        run the job exactly once when we come back, not N times.
    `max_instances=1`: APScheduler-level guard. Belt-and-braces with the
        DAILY_CRON asyncio lock \u2014 if for any reason a previous run is
        still executing when the next fire time arrives, the new fire is
        skipped instead of queued.
    `misfire_grace_time=600`: a 10-minute window. If the host was asleep
        / paused and we wake up <10min after the scheduled time, still
        run. Past that, skip and wait for tomorrow.
    """
    scheduler = AsyncIOScheduler(
        timezone=_timezone_name(),
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 600,
        },
    )
    return scheduler


def register_default_jobs(scheduler: AsyncIOScheduler) -> list[dict[str, Any]]:
    """Wire up the default cron jobs.

    항상 등록되는 잡:
        daily_kis_dart_cron — KIS + DART + FnGuide composite (SCHEDULER_DAILY_CRON)

    조건부 등록 잡:
        daily_hankyung_cron — HANKYUNG_ENABLED=true 일 때만, 전일 한경 리포트 수집.
                              기본 비활성화. docker-compose.gcp-db.yml 에서만 켜짐.

    Returns a list of registration descriptors (id, cron) for logging.
    Idempotent: if a job is already registered, it gets replaced
    (`replace_existing=True`).
    """
    cron_expr = _daily_cron_expression()
    trigger = CronTrigger.from_crontab(cron_expr, timezone=_timezone_name())

    scheduler.add_job(
        _scheduled_daily_cron,
        trigger=trigger,
        id=JOB_ID_DAILY_CRON,
        name="Daily composite backfill (KIS + DART + FnGuide consensus)",
        replace_existing=True,
    )
    registered = [{"id": JOB_ID_DAILY_CRON, "cron": cron_expr}]

    if _hankyung_enabled():
        scheduler.add_job(
            _scheduled_hankyung_cron,
            trigger=trigger,
            id=JOB_ID_HANKYUNG_CRON,
            name="Daily Hankyung consensus (전일 리포트)",
            replace_existing=True,
        )
        registered.append({"id": JOB_ID_HANKYUNG_CRON, "cron": cron_expr})
    else:
        logger.info(
            "[scheduler] hankyung cron not registered "
            "(HANKYUNG_ENABLED is not set to true)"
        )

    if _factor_eval_enabled():
        eval_cron_expr = _factor_eval_cron_expression()
        eval_trigger = CronTrigger.from_crontab(eval_cron_expr, timezone=_timezone_name())
        scheduler.add_job(
            _scheduled_factor_eval_cron,
            trigger=eval_trigger,
            id=JOB_ID_FACTOR_EVAL_CRON,
            name="Weekly factor evaluation (전체 팩터 IC/ICIR/Sharpe)",
            replace_existing=True,
        )
        registered.append({"id": JOB_ID_FACTOR_EVAL_CRON, "cron": eval_cron_expr})
    else:
        logger.info(
            "[scheduler] factor eval cron not registered "
            "(FACTOR_EVAL_ENABLED is not set to true)"
        )

    if _minute_realtime_enabled():
        minute_symbols = _minute_realtime_symbols()
        symbol_desc = (
            f"{len(minute_symbols)}개 종목" if minute_symbols else "전체 활성 종목"
        )
        scheduler.add_job(
            _scheduled_minute_realtime,
            trigger=IntervalTrigger(minutes=1, timezone=_timezone_name()),
            id=JOB_ID_MINUTE_REALTIME,
            name=f"분봉 실시간 수집 ({symbol_desc}, 장중 09:00~15:35 KST)",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=30,
        )
        registered.append({"id": JOB_ID_MINUTE_REALTIME, "interval": "1min"})
        logger.info(
            f"[scheduler] minute realtime registered — {symbol_desc} "
            f"(MINUTE_REALTIME_SYMBOLS={'set' if minute_symbols else 'unset=all'})"
        )
    else:
        logger.info(
            "[scheduler] minute realtime not registered "
            "(MINUTE_REALTIME_ENABLED is not set to true)"
        )

    return registered


def start_scheduler() -> AsyncIOScheduler | None:
    """Build, register, and start the scheduler.

    Returns the scheduler if enabled, or None if disabled via env. The
    return value is stored on `app.state.scheduler` so route handlers
    in src/api/routers/schedule.py can introspect/pause/resume jobs.
    """
    if not _scheduler_enabled():
        logger.info(
            "[scheduler] SCHEDULER_ENABLED=false \u2014 not starting in-process scheduler"
        )
        return None

    scheduler = build_scheduler()
    registered = register_default_jobs(scheduler)
    scheduler.start()
    for r in registered:
        schedule_desc = r.get("cron") or r.get("interval") or "?"
        logger.info(
            f"[scheduler] registered job id={r['id']} schedule={schedule_desc!r} "
            f"tz={_timezone_name()}"
        )
    return scheduler


def stop_scheduler(scheduler: AsyncIOScheduler | None) -> None:
    """Graceful shutdown on app lifespan exit.

    `wait=False` so we don't block the FastAPI shutdown sequence on a
    long-running KIS backfill. In-flight jobs are tracked by JobRegistry
    and will complete (or fail) independently \u2014 the scheduler just
    stops dispatching new fires.
    """
    if scheduler is None:
        return
    try:
        scheduler.shutdown(wait=False)
        logger.info("[scheduler] stopped")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[scheduler] shutdown error: {e}")


__all__ = [
    "JOB_ID_DAILY_CRON",
    "JOB_ID_HANKYUNG_CRON",
    "JOB_ID_FACTOR_EVAL_CRON",
    "JOB_ID_MINUTE_REALTIME",
    "build_scheduler",
    "register_default_jobs",
    "start_scheduler",
    "stop_scheduler",
]
