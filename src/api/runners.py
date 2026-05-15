"""Single entry point for invoking collectors with lock + bookkeeping.

This module is the ONLY place that should call the underlying collector
functions in `src.collectors.*`. Both the HTTP API routers and the
APScheduler jobs go through these `run_*` coroutines. That guarantees:

  1. Every collector run holds the per-collector lock (`collector_lock`
     in src.api.locks). Manual API trigger + scheduled cron can never
     run the same collector at the same instant.
  2. Long-running collectors execute in a worker thread via
     `asyncio.to_thread(...)` so the FastAPI event loop stays responsive
     to status / health requests during a multi-hour KIS backfill.
  3. Async runs are tracked in the JobRegistry; sync runs return the
     collector's return dict directly.

If you need to add a new collector, do it here. Don't sneak around
this layer.
"""
from __future__ import annotations

import asyncio
import time
import traceback
from datetime import date
from typing import Any, Awaitable, Callable

from src.api.jobs import JobRecord, get_registry
from src.api.locks import CollectorBusy, CollectorName, collector_lock
from src.utils.logger import logger


# ---------------------------------------------------------------------------
# Generic wrappers
# ---------------------------------------------------------------------------


async def _run_sync_locked(
    collector: CollectorName,
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Acquire the collector lock, run `fn(*args, **kwargs)` in a thread.

    Returns a `{collector, status, duration_ms, result | error}` dict.
    Does NOT touch the JobRegistry \u2014 use this for short calls returned
    in-band on the HTTP request.

    Raises `CollectorBusy` if the lock can't be acquired immediately;
    routers translate that to a 409.
    """
    t0 = time.time()
    async with collector_lock(collector):
        try:
            # collectors are blocking I/O \u2014 always offload to a thread.
            result = await asyncio.to_thread(fn, *args, **kwargs)
            duration_ms = int((time.time() - t0) * 1000)
            return {
                "collector": collector.value,
                "status": "success",
                "duration_ms": duration_ms,
                "result": _normalize_result(result),
                "error": None,
            }
        except Exception as e:  # noqa: BLE001
            duration_ms = int((time.time() - t0) * 1000)
            logger.exception(f"[{collector.value}] sync run failed: {e}")
            return {
                "collector": collector.value,
                "status": "failed",
                "duration_ms": duration_ms,
                "result": None,
                "error": _format_error(e),
            }


async def _run_async_locked(
    collector: CollectorName,
    params: dict[str, Any],
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> JobRecord:
    """Register a job, kick off background execution, return immediately.

    Lock acquisition happens INSIDE the background task. We register the
    job first so the caller gets a job_id even if the lock is busy \u2014
    the background task will then mark the job REJECTED.

    Why not pre-check the lock and 409 here?
        Because the only safe way to read "is this lock free?" is to
        actually try to acquire it, and acquiring it from the request
        handler doesn't help \u2014 the work won't be done yet by the time
        we return the response. So: register first, attempt the
        acquisition in the worker, surface the busy state via the job
        record. Callers that want immediate-409 semantics can use the
        sync path.
    """
    registry = get_registry()
    record = await registry.create(collector, params)

    async def _worker() -> None:
        try:
            async with collector_lock(collector):
                await registry.mark_running(record.id)
                try:
                    result = await asyncio.to_thread(fn, *args, **kwargs)
                    await registry.mark_success(
                        record.id, _normalize_result(result)
                    )
                    logger.info(
                        f"[{collector.value}] async job {record.id} success"
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        f"[{collector.value}] async job {record.id} failed: {e}"
                    )
                    await registry.mark_failed(record.id, _format_error(e))
        except CollectorBusy as e:
            logger.warning(
                f"[{collector.value}] async job {record.id} rejected: {e.reason}"
            )
            await registry.mark_rejected(record.id, e.reason)
        except Exception as e:  # noqa: BLE001
            # Lock layer failed for some reason other than busy.
            logger.exception(
                f"[{collector.value}] async job {record.id} lock error: {e}"
            )
            await registry.mark_rejected(record.id, _format_error(e))

    # Fire-and-forget. Hold a reference on the task to satisfy asyncio
    # warnings about lost tasks.
    task = asyncio.create_task(_worker(), name=f"job-{record.id}")
    _live_tasks.add(task)
    task.add_done_callback(_live_tasks.discard)

    return record


# Keep strong refs so create_task'd workers aren't GC'd mid-flight.
_live_tasks: set[asyncio.Task[Any]] = set()


def _normalize_result(value: Any) -> dict[str, Any] | None:
    """Force collector return values to a JSON-friendly dict.

    Most collectors already return a dict; a few return None. Anything
    else is wrapped as {value: <repr>} so the API never crashes on an
    unexpected shape.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return {"value": repr(value)}


def _format_error(exc: BaseException) -> str:
    """One-line summary + truncated traceback for JobRecord.error.

    Full traceback is in logs via logger.exception above; this string
    goes into the HTTP response so we cap it.
    """
    head = f"{type(exc).__name__}: {exc}"
    tb = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    return (head if head == tb else f"{head}\n{tb}")[:1500]


# ===========================================================================
# Concrete runners \u2014 one per collector. Both API + Scheduler call these.
# ===========================================================================


# ----- Tickers (sync, short) -----


async def run_tickers_kr(*, desc: bool = True) -> dict[str, Any]:
    """FDR-based Korean ticker master refresh. ~10-30s."""
    from src.collectors.tickers import collect_tickers_fdr
    return await _run_sync_locked(
        CollectorName.TICKERS_KR,
        collect_tickers_fdr,
        ref_date=None,
        desc=desc,
    )


async def run_tickers_us() -> dict[str, Any]:
    """NASDAQ Trader US ticker master refresh. ~10s."""
    from src.collectors.tickers_us import collect_us_tickers
    return await _run_sync_locked(
        CollectorName.TICKERS_US,
        collect_us_tickers,
    )


# ----- DART corp_codes (sync, short with freshness check) -----


async def run_dart_corp_codes(
    *,
    force: bool = False,
    stale_after_days: int | None = None,
) -> dict[str, Any]:
    """DART corp_code master refresh. Skips if fresh \u2014 usually instant."""
    from src.collectors.dart_corp_codes import collect_corp_codes
    return await _run_sync_locked(
        CollectorName.DART_CORP_CODES,
        collect_corp_codes,
        force=force,
        stale_after_days=stale_after_days,
    )


# ----- Daily prices (async, long) -----


async def submit_daily_fdr(
    *,
    symbols: list[str] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    days: int | None = None,
    markets: list[str] | None = None,
    skip_done: bool = True,
) -> JobRecord:
    """Korean daily prices via FinanceDataReader. Long-running.

    Implementation note:
        The collector exposes two paths \u2014 `backfill_symbols` (pinned
        list) and `backfill_active_universe` (everything). We pick
        based on whether `symbols` was provided, matching the CLI's
        behavior.
    """
    from src.collectors.daily_fdr import (
        backfill_active_universe,
        backfill_symbols,
    )

    params: dict[str, Any] = {
        "symbols": symbols,
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "days": days,
        "markets": markets,
        "skip_done": skip_done,
    }

    if symbols:
        return await _run_async_locked(
            CollectorName.DAILY_FDR, params, backfill_symbols,
            symbols=symbols, start_date=start_date, end_date=end_date,
            days=days, skip_done=skip_done,
        )
    return await _run_async_locked(
        CollectorName.DAILY_FDR, params, backfill_active_universe,
        start_date=start_date, end_date=end_date, days=days,
        markets=markets, skip_done=skip_done,
    )


async def submit_daily_kis(
    *,
    symbols: list[str] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    days: int | None = None,
    skip_done: bool = True,
    fetch_snapshot: bool = True,
    request_delay: float | None = None,
) -> JobRecord:
    """Korean daily prices via KIS. Long-running."""
    from src.collectors.daily_kis import backfill_symbols

    params: dict[str, Any] = {
        "symbols": symbols,
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "days": days,
        "skip_done": skip_done,
        "fetch_snapshot": fetch_snapshot,
        "request_delay": request_delay,
    }
    return await _run_async_locked(
        CollectorName.DAILY_KIS, params, backfill_symbols,
        symbols=symbols, start_date=start_date, end_date=end_date,
        days=days, skip_done=skip_done, fetch_snapshot=fetch_snapshot,
        request_delay=request_delay,
    )


async def submit_daily_us(
    *,
    symbols: list[str] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    days: int | None = None,
    exchanges: list[str] | None = None,
    security_types: list[str] | None = None,
    skip_done: bool = True,
) -> JobRecord:
    """US daily prices via yfinance. Long-running but faster than Korea."""
    from src.collectors.daily_us_yf import (
        backfill_active_universe,
        backfill_symbols,
    )

    params: dict[str, Any] = {
        "symbols": symbols,
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "days": days,
        "exchanges": exchanges,
        "security_types": security_types,
        "skip_done": skip_done,
    }

    if symbols:
        return await _run_async_locked(
            CollectorName.DAILY_US_YF, params, backfill_symbols,
            symbols=symbols, start_date=start_date, end_date=end_date,
            days=days, skip_done=skip_done,
        )
    return await _run_async_locked(
        CollectorName.DAILY_US_YF, params, backfill_active_universe,
        start_date=start_date, end_date=end_date, days=days,
        exchanges=exchanges, security_types=security_types,
        skip_done=skip_done,
    )


# ----- DART disclosures (async, medium) -----


async def submit_dart_disclosures(
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    days: int | None = None,
    kinds: list[str] | None = None,
    listed_only: bool = True,
    skip_done: bool = True,
) -> JobRecord:
    """DART disclosure feed for a date range."""
    from src.collectors.dart_disclosures import (
        ALL_KINDS,
        DEFAULT_KINDS,
        collect_disclosures,
    )

    # Translate ['ALL'] sentinel to the full kind set; otherwise pass
    # through (the collector validates membership).
    if kinds is None:
        effective_kinds: tuple[str, ...] = DEFAULT_KINDS
    elif len(kinds) == 1 and kinds[0].upper() == "ALL":
        effective_kinds = ALL_KINDS
    else:
        effective_kinds = tuple(kinds)

    params: dict[str, Any] = {
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "days": days,
        "kinds": list(effective_kinds),
        "listed_only": listed_only,
        "skip_done": skip_done,
    }
    return await _run_async_locked(
        CollectorName.DART_DISCLOSURES, params, collect_disclosures,
        start_date=start_date, end_date=end_date, days=days,
        kinds=effective_kinds, listed_only=listed_only,
        skip_done=skip_done,
    )


# ----- DART financials & indicators (async, very long) -----

async def submit_dart_financials(
    *,
    start_year: int = 2020,
    end_year: int | None = None,
    reprt_codes: list[str] | None = None,
    fs_divs: list[str] | None = None,
    corp_codes: list[str] | None = None,
    skip_done: bool = True,
    max_calls: int | None = None,
) -> JobRecord:
    from src.collectors.dart_financials import (
        DEFAULT_FS_DIVS,
        REPRT_CODES,
        collect_financials,
    )

    effective_reprt = tuple(reprt_codes) if reprt_codes else tuple(REPRT_CODES.keys())
    effective_fs_divs = tuple(fs_divs) if fs_divs else DEFAULT_FS_DIVS

    params: dict[str, Any] = {
        "start_year": start_year,
        "end_year": end_year,
        "reprt_codes": list(effective_reprt),
        "fs_divs": list(effective_fs_divs),
        "corp_codes": corp_codes,
        "skip_done": skip_done,
        "max_calls": max_calls,
    }
    return await _run_async_locked(
        CollectorName.DART_FINANCIALS, params, collect_financials,
        start_year=start_year, end_year=end_year,
        reprt_codes=effective_reprt, fs_divs=effective_fs_divs,
        corp_codes=corp_codes, skip_done=skip_done, max_calls=max_calls,
    )


async def submit_dart_indicators(
    *,
    start_year: int = 2020,
    end_year: int | None = None,
    reprt_codes: list[str] | None = None,
    fs_divs: list[str] | None = None,
    idx_cl_codes: list[str] | None = None,
    corp_codes: list[str] | None = None,
    skip_done: bool = True,
    max_calls: int | None = None,
) -> JobRecord:
    from src.collectors.dart_indicators import (
        DEFAULT_FS_DIVS,
        IDX_CL_CODES,
        REPRT_CODES,
        collect_indicators,
    )

    effective_reprt = tuple(reprt_codes) if reprt_codes else tuple(REPRT_CODES.keys())
    effective_fs_divs = tuple(fs_divs) if fs_divs else DEFAULT_FS_DIVS
    effective_idx_cl = (
        tuple(idx_cl_codes) if idx_cl_codes else tuple(IDX_CL_CODES.keys())
    )

    params: dict[str, Any] = {
        "start_year": start_year,
        "end_year": end_year,
        "reprt_codes": list(effective_reprt),
        "fs_divs": list(effective_fs_divs),
        "idx_cl_codes": list(effective_idx_cl),
        "corp_codes": corp_codes,
        "skip_done": skip_done,
        "max_calls": max_calls,
    }
    return await _run_async_locked(
        CollectorName.DART_INDICATORS, params, collect_indicators,
        start_year=start_year, end_year=end_year,
        reprt_codes=effective_reprt, fs_divs=effective_fs_divs,
        idx_cl_codes=effective_idx_cl, corp_codes=corp_codes,
        skip_done=skip_done, max_calls=max_calls,
    )


# ----- FnGuide consensus (async, long) -----


async def submit_consensus_fnguide(
    *,
    symbols: list[str] | None = None,
    as_of_date: date | None = None,
    markets: list[str] | None = None,
    skip_done: bool = True,
    request_delay: float | None = None,
) -> JobRecord:
    """FnGuide consensus snapshot for one as_of_date.

    개인용 비공개 한정. 자세한 정책은
    src/collectors/consensus_fnguide.py 모듈 docstring 참고. .env 에
    FNGUIDE_CONSENT_ACK=1 이 없으면 collector 가 RuntimeError 로 거부하고,
    그 예외는 이 함수의 _run_async_locked 이 잡아서 JobRecord 를
    'failed' 로 마킹한다.

    Implementation note:
        CLI 와 동일하게 symbols 의 유무에 따라 backfill_symbols 또는
        backfill_active_universe 을 선택. as_of_date 는 None 이면
        수집기가 자체적으로 오늘로 결정함.
    """
    from src.collectors.consensus_fnguide import (
        backfill_active_universe,
        backfill_symbols,
    )

    params: dict[str, Any] = {
        "symbols": symbols,
        "as_of_date": as_of_date.isoformat() if as_of_date else None,
        "markets": markets,
        "skip_done": skip_done,
        "request_delay": request_delay,
    }

    if symbols:
        return await _run_async_locked(
            CollectorName.CONSENSUS_FNGUIDE, params, backfill_symbols,
            symbols=symbols, as_of_date=as_of_date,
            skip_done=skip_done, request_delay=request_delay,
        )
    return await _run_async_locked(
        CollectorName.CONSENSUS_FNGUIDE, params, backfill_active_universe,
        as_of_date=as_of_date, markets=markets,
        skip_done=skip_done, request_delay=request_delay,
    )


# ----- Composite: daily cron (KIS + DART + FnGuide consensus) -----


# Recognized `only` values for the composite cron. None = run all three.
_DAILY_CRON_ONLY_VALUES = (None, "kis", "dart", "fnguide")


def _daily_cron_blocking(
    *,
    end_date: date,
    days: int,
    only: str | None,
    fetch_snapshot: bool,
    skip_done: bool,
) -> dict[str, Any]:
    """Run the composite daily backfill (KIS + DART + FnGuide consensus).

    Each step is isolated: a failure in one does NOT block the others.
    The shared `DAILY_CRON` lock (held by `submit_daily_cron`) keeps
    parallel triggers of the same composite from colliding, but the
    inner collectors are called directly here — they don't reacquire
    their own locks.

    The FnGuide step refuses to run unless `FNGUIDE_CONSENT_ACK=1` is
    set in .env. That refusal is treated as a soft skip (recorded in
    `summary['fnguide_consensus']` as 'skipped_no_consent') rather than
    a failure, because not every operator of this code base will have
    opted into the FnGuide scrape.
    """
    from datetime import timedelta

    from src.collectors.daily_kis import backfill_symbols as kis_backfill
    from src.collectors.dart_corp_codes import collect_corp_codes
    from src.collectors.dart_disclosures import (
        DEFAULT_KINDS,
        collect_disclosures,
    )

    summary: dict[str, Any] = {
        "end_date": end_date.isoformat(),
        "days": days,
        "only": only,
        "kis": None,
        "dart_corp_codes": None,
        "dart_disclosures": None,
        "fnguide_consensus": None,
        "errors": [],
    }

    start_date = end_date - timedelta(days=max(0, days - 1))

    # --- KIS step ---
    if only in (None, "kis"):
        try:
            kis_backfill(
                symbols=None,
                start_date=start_date,
                end_date=end_date,
                skip_done=skip_done,
                fetch_snapshot=fetch_snapshot,
            )
            summary["kis"] = "ok"
        except Exception as e:  # noqa: BLE001
            summary["kis"] = "failed"
            summary["errors"].append(f"kis: {e}")
            logger.exception(f"[daily-cron] KIS step failed: {e}")

    # --- DART step ---
    if only in (None, "dart"):
        try:
            cc_result = collect_corp_codes(force=False)
            summary["dart_corp_codes"] = cc_result
        except Exception as e:  # noqa: BLE001
            summary["dart_corp_codes"] = "failed"
            summary["errors"].append(f"dart_corp_codes: {e}")
            logger.exception(f"[daily-cron] DART corp_codes failed: {e}")

        try:
            disc_result = collect_disclosures(
                start_date=start_date,
                end_date=end_date,
                kinds=DEFAULT_KINDS,
                listed_only=True,
                skip_done=skip_done,
            )
            summary["dart_disclosures"] = disc_result
        except Exception as e:  # noqa: BLE001
            summary["dart_disclosures"] = "failed"
            summary["errors"].append(f"dart_disclosures: {e}")
            logger.exception(f"[daily-cron] DART disclosures failed: {e}")

    # --- FnGuide consensus step ---
    # 개인용 비공개 수집이므로 .env 의 FNGUIDE_CONSENT_ACK=1 이 없으면
    # 조용히 건너뛴. 에러로 기록하지 않는다 — KIS/DART 만 쓰는 운영자가
    # cron 에서 항상 "failed" 를 보게 되는 것은 잡음.
    if only in (None, "fnguide"):
        from src.config import get_fnguide_settings
        if not get_fnguide_settings().consent_ack:
            summary["fnguide_consensus"] = "skipped_no_consent"
            logger.info(
                "[daily-cron] FnGuide step skipped: "
                "FNGUIDE_CONSENT_ACK not set to 1 in .env"
            )
        else:
            from src.collectors.consensus_fnguide import (
                backfill_active_universe as fnguide_backfill,
            )
            try:
                # as_of_date = 수집기 자체 기본값 = 오늘. cron 이 03:00 KST 에
                # 돌면 '오늘' 은 장 시작 전 새벽을 포함하며, 이건 의도된
                # 동작 — 어제 발행된 리포트의 최종 컨센서스가 이 시간에 반영됨.
                fnguide_result = fnguide_backfill(
                    as_of_date=None,
                    skip_done=skip_done,
                )
                summary["fnguide_consensus"] = fnguide_result
            except Exception as e:  # noqa: BLE001
                summary["fnguide_consensus"] = "failed"
                summary["errors"].append(f"fnguide_consensus: {e}")
                logger.exception(
                    f"[daily-cron] FnGuide consensus step failed: {e}"
                )

    return summary


async def submit_daily_cron(
    *,
    end_date: date | None = None,
    days: int = 1,
    only: str | None = None,
    fetch_snapshot: bool = True,
    skip_done: bool = True,
) -> JobRecord:
    """Async submit of the composite daily cron job.

    Composition: KIS daily prices + DART corp_codes/disclosures +
    FnGuide consensus snapshot. Each step is independent; one failure
    does not abort the others. The `only` parameter narrows to a single
    step ("kis" | "dart" | "fnguide"); None = run all three.

    Uses the `DAILY_CRON` lock to serialize against itself only. Inner
    collectors do NOT take their own locks because they're called
    directly from `_daily_cron_blocking` inside this lock. To prevent a
    user from manually triggering `daily_kis` or `consensus_fnguide`
    mid-cron, the routes for those individual collectors should ALSO
    try-acquire `DAILY_CRON` \u2014 see how `run_*` are wired in
    src/api/routers/collect.py.
    """
    from datetime import date as _date, timedelta as _timedelta

    effective_end = end_date or (_date.today() - _timedelta(days=1))
    params = {
        "end_date": effective_end.isoformat(),
        "days": days,
        "only": only,
        "fetch_snapshot": fetch_snapshot,
        "skip_done": skip_done,
    }
    return await _run_async_locked(
        CollectorName.DAILY_CRON, params, _daily_cron_blocking,
        end_date=effective_end, days=days, only=only,
        fetch_snapshot=fetch_snapshot, skip_done=skip_done,
    )


__all__ = [
    # sync runs
    "run_tickers_kr",
    "run_tickers_us",
    "run_dart_corp_codes",
    # async runs
    "submit_daily_fdr",
    "submit_daily_kis",
    "submit_daily_us",
    "submit_dart_disclosures",
    "submit_dart_financials",
    "submit_dart_indicators",
    "submit_consensus_fnguide",
    "submit_daily_cron",
]
