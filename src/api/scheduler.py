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

import os
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.api.runners import submit_daily_cron
from src.utils.logger import logger


# Job IDs (stable strings so /schedule endpoints can address them).
JOB_ID_DAILY_CRON = "daily_kis_dart_cron"


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
    """Wire up the default 03:00 KST composite cron job.

    Returns a list of registration descriptors (id, cron) for logging.
    Idempotent: if the job is already registered, it gets replaced
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
    return [{"id": JOB_ID_DAILY_CRON, "cron": cron_expr}]


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
        logger.info(
            f"[scheduler] registered job id={r['id']} cron={r['cron']!r} "
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
    "build_scheduler",
    "register_default_jobs",
    "start_scheduler",
    "stop_scheduler",
]
