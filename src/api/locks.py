"""Per-collector locks shared by the API trigger layer and the scheduler.

Why this exists:
    The default daily cron runs in-process via APScheduler. The HTTP API
    exposes manual triggers for the same collectors. Without a shared
    locking layer, both can launch the same collector at the same
    moment and end up:
      - racing on `collection_log` (`skip_done` becomes unreliable)
      - double-spending KIS/DART rate-limit budget
      - corrupting the KIS token file (writes from two threads)
      - emitting duplicate work that the COPY+ON CONFLICT path silently
        masks but which doubles wall time for no reason

Lock layers (acquire in this order — release in reverse):

    1. asyncio.Lock, per-collector, process-local
       Cheap, fast, and the *primary* defense. Because the scheduler
       and the API run in the SAME asyncio event loop in the SAME
       process, this is enough on its own to prevent every concurrency
       bug listed above.

    2. PostgreSQL advisory lock, per-collector, cluster-wide
       A secondary safety net for the case where someone still has the
       old `python -m src.main` cron registered in Windows Task
       Scheduler, OR where the operator runs `python -m src.pipelines.*`
       manually from another terminal. Those processes can't see our
       asyncio.Lock, but `pg_try_advisory_lock` (non-blocking) lets us
       detect them and 409 cleanly. Each collector has a fixed bigint
       key (see `_ADVISORY_KEYS` below). The lock is session-scoped:
       held by the psycopg connection until we release it or the
       connection closes.

Acquire policy:
    NON-BLOCKING. If either lock can't be obtained immediately we raise
    `CollectorBusy`. Callers (API + scheduler) decide whether that
    becomes a 409 or a "skipped this tick" log line — that decision is
    *not* in this module.

Why these advisory keys:
    Postgres advisory locks use a 64-bit signed integer (or a pair of
    int32s). We hash the collector name into a stable int32 with the
    high-half as a magic prefix (0x5754_0000 = "ST\\0\\0", short for
    stock_trader) to avoid collision with anything else in the cluster.
    See `_collector_key` for the construction.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
from enum import Enum
from typing import AsyncIterator

import psycopg

from src.db.connection import raw_connection
from src.utils.logger import logger


class CollectorName(str, Enum):
    """Canonical collector identifiers.

    Values match `COLLECTOR_NAME` constants inside each collector module
    and the `collector` column written to `collection_log` so traces line
    up across all three sources.
    """

    # Master tables
    TICKERS_US = "tickers_us"        # collect_us_tickers
    DART_CORP_CODES = "dart_corp_codes"

    # Daily prices
    DAILY_KIS = "daily_kis"
    DAILY_US_YF = "daily_us_yf"

    # DART
    DART_DISCLOSURES = "dart_disclosures"
    DART_FINANCIALS = "dart_financials"
    DART_INDICATORS = "dart_indicators"

    # Consensus / analyst data
    CONSENSUS_FNGUIDE = "consensus_fnguide"
    CONSENSUS_HANKYUNG = "consensus_hankyung"

    # Composite — the default daily cron (KIS + DART + FnGuide consensus).
    # Holds while any of the three composite steps is running so manual
    # triggers for daily_kis / dart_* / consensus_fnguide will 409 during
    # the cron's window. The inner collectors do NOT acquire their own
    # locks (they're called directly from _daily_cron_blocking), so this
    # composite lock is the only thing guarding them during the cron.
    DAILY_CRON = "daily_cron"

    # Research — factor signal computation + evaluation
    FACTOR_EVAL = "factor_eval"

    # Minute prices — KIS 분봉 수집
    MINUTE_KIS = "minute_kis"


# Magic prefix for the advisory-lock key high half: ASCII "ST\\0\\0".
# Keeps our keys in a recognizable, project-scoped namespace inside the
# cluster — easy to spot in pg_locks if anyone ever inspects.
_ADVISORY_NAMESPACE = 0x5754_0000


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CollectorBusy(RuntimeError):
    """Raised when a collector lock is already held by another caller.

    Callers translate this to a 409 (HTTP) or a warning log (scheduler).
    """

    def __init__(self, collector: CollectorName, *, reason: str) -> None:
        self.collector = collector
        self.reason = reason
        super().__init__(f"{collector.value} is busy: {reason}")


# ---------------------------------------------------------------------------
# In-process asyncio locks
# ---------------------------------------------------------------------------


# One asyncio.Lock per collector. Created lazily because asyncio.Lock
# binds to the running event loop on first use, and at import time
# there is no loop yet. The dict is process-local and that is exactly
# what we want — the scheduler and the API share this module.
_async_locks: dict[CollectorName, asyncio.Lock] = {}


def _get_async_lock(collector: CollectorName) -> asyncio.Lock:
    lock = _async_locks.get(collector)
    if lock is None:
        lock = asyncio.Lock()
        _async_locks[collector] = lock
    return lock


def is_locally_busy(collector: CollectorName) -> bool:
    """True iff this process is currently running this collector.

    Useful for `GET /status` style endpoints. Not used to make
    acquisition decisions (those go through `collector_lock`).
    """
    lock = _async_locks.get(collector)
    return lock is not None and lock.locked()


def busy_collectors() -> list[CollectorName]:
    """Snapshot of which collectors are currently running in this process."""
    return [c for c, lock in _async_locks.items() if lock.locked()]


# ---------------------------------------------------------------------------
# PostgreSQL advisory locks
# ---------------------------------------------------------------------------


def _collector_key(collector: CollectorName) -> int:
    """Map a collector name to a stable signed 64-bit advisory-lock key.

    Construction:
        high32 = _ADVISORY_NAMESPACE   (0x57540000)
        low32  = first 4 bytes of sha256(name), interpreted as uint32
        key    = (high32 << 32) | low32   then sign-cast to int64

    Result is deterministic across processes and restarts, project-
    scoped (the namespace prefix), and extremely unlikely to collide
    with any other advisory-lock user in the same cluster.
    """
    digest = hashlib.sha256(collector.value.encode("utf-8")).digest()
    low32 = int.from_bytes(digest[:4], "big", signed=False)
    unsigned = (_ADVISORY_NAMESPACE << 32) | low32
    # Postgres pg_try_advisory_lock(bigint) takes a signed int64. Cast.
    if unsigned >= 2 ** 63:
        return unsigned - 2 ** 64
    return unsigned


def _try_acquire_pg(conn: psycopg.Connection, collector: CollectorName) -> bool:
    """Run pg_try_advisory_lock; True on success, False if already held."""
    key = _collector_key(collector)
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
        row = cur.fetchone()
    return bool(row and row[0])


def _release_pg(conn: psycopg.Connection, collector: CollectorName) -> None:
    """Release the advisory lock. Logs but does not raise on failure
    (the lock is session-scoped anyway, and closing the connection
    releases it)."""
    key = _collector_key(collector)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"pg_advisory_unlock({collector.value}) failed: {e}")


# ---------------------------------------------------------------------------
# Public acquisition context
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def collector_lock(
    collector: CollectorName,
    *,
    use_pg_advisory: bool = True,
) -> AsyncIterator[None]:
    """Acquire BOTH the in-process and PG advisory lock for `collector`.

    Non-blocking: if either lock is held, raises `CollectorBusy`. The
    caller MUST be ready to handle that — there is no "wait my turn"
    semantics here. We chose that on purpose: a queued trigger creates
    an invisible backlog and turns "is this safe" reasoning into a
    multi-tick puzzle. Better to refuse loudly and let the caller retry
    when they actually want to.

    Args:
        collector: which collector slot to lock.
        use_pg_advisory: opt-out only for tests that spin up the lock
            machinery without a live DB. Production always leaves this
            on so external processes (legacy cron, manual `python -m
            src.pipelines.*`) get detected.

    Yields:
        None. Use as:
            async with collector_lock(CollectorName.DAILY_KIS):
                await run_in_thread(backfill_symbols, ...)
    """
    async_lock = _get_async_lock(collector)

    # Layer 1: asyncio lock, non-blocking.
    if async_lock.locked():
        raise CollectorBusy(
            collector,
            reason="another in-process task is already running this collector",
        )
    # acquire() on an unlocked asyncio.Lock returns immediately, but we
    # still go through the API to set the locked flag atomically.
    await async_lock.acquire()

    pg_conn: psycopg.Connection | None = None
    try:
        # Layer 2: PG advisory lock, non-blocking.
        if use_pg_advisory:
            # Open a dedicated connection so the lock is session-scoped
            # to JUST this collector run. raw_connection() commits and
            # closes on exit, which would drop the advisory lock for us;
            # but we want to manage the connection's lifetime ourselves
            # to keep the lock held across the whole `yield`. So we step
            # outside the context manager and open psycopg directly.
            from src.config import get_db_settings  # local import: avoid cycle
            dsn = get_db_settings().psycopg_dsn
            pg_conn = psycopg.connect(dsn, autocommit=True)
            acquired = _try_acquire_pg(pg_conn, collector)
            if not acquired:
                # Release async lock before raising so the caller can retry.
                async_lock.release()
                pg_conn.close()
                pg_conn = None
                raise CollectorBusy(
                    collector,
                    reason=(
                        "another OS process holds the PostgreSQL advisory "
                        "lock for this collector (legacy cron or manual "
                        "pipeline run?)"
                    ),
                )

        logger.debug(f"acquired collector lock: {collector.value}")
        yield
    finally:
        # Reverse order: release PG first, then asyncio.
        if pg_conn is not None:
            _release_pg(pg_conn, collector)
            pg_conn.close()
        if async_lock.locked():
            async_lock.release()
        logger.debug(f"released collector lock: {collector.value}")


__all__ = [
    "CollectorName",
    "CollectorBusy",
    "collector_lock",
    "is_locally_busy",
    "busy_collectors",
]
