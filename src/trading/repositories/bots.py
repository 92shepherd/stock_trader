"""CRUD for the 6 EOD-bot tables.

Naming follows the existing repository conventions in
`src/db/repositories.py`: small reads/writes via SQLAlchemy ORM,
plus a few text() statements for atomic transitions.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from src.db.connection import get_engine, session_scope
from src.db.models import (
    EodBot,
    EodBotDailyPnL,
    EodBotOrder,
    EodBotPosition,
    EodBotRun,
    EodBotSpecHistory,
)
from src.trading.simulator.broker import FillPlan
from src.utils.logger import logger


# ---------------------------------------------------------------------------
# Errors used by the REST layer for clean 4xx mapping
# ---------------------------------------------------------------------------


class BotNotFoundError(LookupError):
    pass


class BotNameTakenError(ValueError):
    pass


class InvalidBotStateError(RuntimeError):
    """e.g. trying to start a STOPPED bot, or tick a non-RUNNING bot."""


class DuplicateTickError(RuntimeError):
    """Bot already has a tick on this decision_date."""


# ---------------------------------------------------------------------------
# Bot creation / lifecycle
# ---------------------------------------------------------------------------


def create_bot(
    *,
    name: str,
    strategy_kind: str,
    universe: str,
    seed_cash: Decimal,
    declarative_spec: dict | None = None,
    plugin_strategy_id: str | None = None,
    required_factors: list[str] | None = None,
    notes: str | None = None,
) -> EodBot:
    """Create a bot in PENDING state with seed_cash.

    Also writes the first spec_history row (spec_version=1).

    Raises:
        BotNameTakenError: name already exists.
        ValueError: validation failure on inputs.
    """
    if strategy_kind not in ("declarative", "plugin"):
        raise ValueError(
            f"strategy_kind must be 'declarative' or 'plugin', got '{strategy_kind}'"
        )
    if strategy_kind == "declarative" and declarative_spec is None:
        raise ValueError("declarative_spec required when strategy_kind='declarative'")
    if strategy_kind == "plugin" and not plugin_strategy_id:
        raise ValueError("plugin_strategy_id required when strategy_kind='plugin'")
    if seed_cash <= 0:
        raise ValueError(f"seed_cash must be > 0, got {seed_cash}")

    with session_scope() as session:
        if session.execute(
            select(EodBot.bot_id).where(EodBot.name == name)
        ).first() is not None:
            raise BotNameTakenError(f"bot name '{name}' already exists")

        bot = EodBot(
            name=name,
            state="PENDING",
            strategy_kind=strategy_kind,
            declarative_spec=declarative_spec,
            plugin_strategy_id=plugin_strategy_id,
            universe=universe,
            seed_cash=seed_cash,
            cash=seed_cash,
            holdings_value=Decimal("0"),
            total_value=seed_cash,
            notes=notes,
        )
        session.add(bot)
        session.flush()

        spec = EodBotSpecHistory(
            bot_id=bot.bot_id,
            spec_version=1,
            strategy_kind=strategy_kind,
            spec_json=declarative_spec,
            plugin_strategy_id=plugin_strategy_id,
            required_factors=required_factors,
            created_by="api",
            reason="initial",
        )
        session.add(spec)
        session.flush()
        return bot


def start_bot(bot_id: uuid.UUID) -> EodBot:
    """Transition PENDING → RUNNING. No-op if already RUNNING.

    Raises:
        BotNotFoundError, InvalidBotStateError.
    """
    with session_scope() as session:
        bot = _get_bot_for_update(session, bot_id)
        if bot.state == "STOPPED":
            raise InvalidBotStateError(
                f"bot {bot.name} is STOPPED — cannot restart "
                "(create a new bot instead)"
            )
        if bot.state == "PENDING":
            bot.state = "RUNNING"
            bot.started_at = datetime.now(timezone.utc)
        return bot


def stop_bot(
    bot_id: uuid.UUID,
    reason: str | None = None,
) -> EodBot:
    """Transition any state → STOPPED. Records final PnL/return.

    After STOPPED, no daily tick will create any orders.
    """
    with session_scope() as session:
        bot = _get_bot_for_update(session, bot_id)
        if bot.state == "STOPPED":
            return bot

        final_total = bot.cash + bot.holdings_value
        final_pnl = final_total - bot.seed_cash
        final_return = (
            (final_pnl / bot.seed_cash) if bot.seed_cash > 0 else Decimal("0")
        )
        bot.state = "STOPPED"
        bot.stopped_at = datetime.now(timezone.utc)
        bot.final_total_value = final_total
        bot.final_pnl = final_pnl
        bot.final_return_pct = final_return
        if reason:
            bot.notes = (bot.notes or "") + f"\n[stop] {reason}"
        return bot


# ---------------------------------------------------------------------------
# Bot queries
# ---------------------------------------------------------------------------


def get_bot(bot_id: uuid.UUID) -> EodBot:
    """Fetch by ID. Raises BotNotFoundError."""
    with session_scope() as session:
        bot = session.get(EodBot, bot_id)
        if bot is None:
            raise BotNotFoundError(f"bot_id {bot_id} not found")
        return bot


def get_bot_by_name(name: str) -> EodBot | None:
    """Fetch by name. Returns None if missing."""
    with session_scope() as session:
        bot = session.execute(
            select(EodBot).where(EodBot.name == name)
        ).scalar_one_or_none()
        return bot


def list_bots(
    state: str | None = None,
    limit: int = 100,
) -> list[EodBot]:
    """List bots, newest first. Optional state filter."""
    with session_scope() as session:
        stmt = select(EodBot)
        if state is not None:
            stmt = stmt.where(EodBot.state == state)
        stmt = stmt.order_by(EodBot.created_at.desc()).limit(limit)
        bots = list(session.execute(stmt).scalars().all())
        return bots


def get_running_bots() -> list[EodBot]:
    """All bots currently in RUNNING state — input to the daily cron."""
    return list_bots(state="RUNNING", limit=10_000)


# ---------------------------------------------------------------------------
# Spec history
# ---------------------------------------------------------------------------


def get_current_spec(bot_id: uuid.UUID) -> EodBotSpecHistory | None:
    """Most recent spec_history row for `bot_id`."""
    with session_scope() as session:
        stmt = (
            select(EodBotSpecHistory)
            .where(EodBotSpecHistory.bot_id == bot_id)
            .order_by(EodBotSpecHistory.spec_version.desc())
            .limit(1)
        )
        spec = session.execute(stmt).scalar_one_or_none()
        return spec


def upsert_spec_history(
    *,
    bot_id: uuid.UUID,
    strategy_kind: str,
    spec_json: dict | None,
    plugin_strategy_id: str | None,
    required_factors: list[str] | None,
    created_by: str = "api",
    reason: str | None = None,
) -> EodBotSpecHistory:
    """Append a new spec_history row (next version), update bot mirror."""
    with session_scope() as session:
        bot = _get_bot_for_update(session, bot_id)
        if bot.state == "STOPPED":
            raise InvalidBotStateError(
                f"bot {bot.name} is STOPPED — cannot change spec"
            )

        prev = session.execute(
            select(EodBotSpecHistory.spec_version)
            .where(EodBotSpecHistory.bot_id == bot_id)
            .order_by(EodBotSpecHistory.spec_version.desc())
            .limit(1)
        ).first()
        next_v = (prev[0] + 1) if prev else 1

        spec = EodBotSpecHistory(
            bot_id=bot_id,
            spec_version=next_v,
            strategy_kind=strategy_kind,
            spec_json=spec_json,
            plugin_strategy_id=plugin_strategy_id,
            required_factors=required_factors,
            created_by=created_by,
            reason=reason,
        )
        session.add(spec)

        bot.strategy_kind = strategy_kind
        bot.declarative_spec = spec_json
        bot.plugin_strategy_id = plugin_strategy_id

        session.flush()
        return spec


# ---------------------------------------------------------------------------
# Tick lifecycle (run log + orders + positions + pnl)
# ---------------------------------------------------------------------------


def insert_run_log(
    *,
    bot_id: uuid.UUID,
    decision_date: date,
    spec_history_id: uuid.UUID | None,
    universe_size: int | None = None,
) -> EodBotRun:
    """Open a new run log row.

    Raises:
        DuplicateTickError: a run already exists for (bot_id, decision_date).
    """
    with session_scope() as session:
        existing = session.execute(
            select(EodBotRun)
            .where(EodBotRun.bot_id == bot_id)
            .where(EodBotRun.decision_date == decision_date)
        ).scalar_one_or_none()
        if existing is not None:
            raise DuplicateTickError(
                f"bot {bot_id} already has a tick on {decision_date} "
                f"(run_id={existing.run_id}, status={existing.status})"
            )

        run = EodBotRun(
            bot_id=bot_id,
            decision_date=decision_date,
            spec_history_id=spec_history_id,
            status="SUCCESS",
            universe_size=universe_size,
        )
        session.add(run)
        session.flush()
        return run


def update_run_log(
    *,
    run_id: uuid.UUID,
    status: str,
    finished_at: datetime | None = None,
    duration_ms: int | None = None,
    universe_size: int | None = None,
    eligible_size: int | None = None,
    scored_size: int | None = None,
    factor_coverage: dict[str, int] | None = None,
    target_size: int | None = None,
    n_buys: int | None = None,
    n_sells: int | None = None,
    skip_reason: str | None = None,
    error_message: str | None = None,
) -> None:
    """Finalize a run log row.

    All status-specific fields are optional kwargs — the SKIPPED / SUCCESS /
    FAILED branches in the runner can pass whatever diagnostic info they have
    without needing to know the schema in advance. `universe_size` is
    included here (in addition to `insert_run_log`) because in SKIPPED
    branches we sometimes only learn it after the initial insert.
    """
    if status not in ("SUCCESS", "SKIPPED", "FAILED"):
        raise ValueError(f"status must be SUCCESS/SKIPPED/FAILED, got {status}")
    with session_scope() as session:
        run = session.get(EodBotRun, run_id)
        if run is None:
            raise BotNotFoundError(f"run_id {run_id} not found")
        run.status = status
        run.finished_at = finished_at or datetime.now(timezone.utc)
        if duration_ms is not None:
            run.duration_ms = duration_ms
        if universe_size is not None:
            run.universe_size = universe_size
        if eligible_size is not None:
            run.eligible_size = eligible_size
        if scored_size is not None:
            run.scored_size = scored_size
        if factor_coverage is not None:
            run.factor_coverage = factor_coverage
        if target_size is not None:
            run.target_size = target_size
        if n_buys is not None:
            run.n_buys = n_buys
        if n_sells is not None:
            run.n_sells = n_sells
        if skip_reason is not None:
            run.skip_reason = skip_reason
        if error_message is not None:
            run.error_message = error_message


# ---------------------------------------------------------------------------
# Orders / positions / cash
# ---------------------------------------------------------------------------


def get_open_positions(bot_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """Return current open positions from the most recent snapshot.

    Returns dict[symbol -> {quantity, avg_cost, last_date}].
    """
    sql = text("""
        WITH latest AS (
            SELECT bot_id, symbol, MAX(date) AS max_date
              FROM eod_bot_positions
             WHERE bot_id = :bot
             GROUP BY bot_id, symbol
        )
        SELECT p.symbol, p.quantity, p.avg_cost, p.date
          FROM eod_bot_positions p
          JOIN latest l
            ON l.bot_id = p.bot_id
           AND l.symbol = p.symbol
           AND l.max_date = p.date
         WHERE p.bot_id = :bot
           AND p.quantity > 0
    """)
    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"bot": bot_id}).all()
    return {
        r[0]: {
            "quantity": int(r[1]),
            "avg_cost": float(r[2]),
            "last_date": r[3],
        }
        for r in rows
    }


def insert_orders(
    *,
    bot_id: uuid.UUID,
    run_id: uuid.UUID,
    spec_history_id: uuid.UUID | None,
    decision_date: date,
    fills: list[FillPlan],
    composite_scores: dict[str, float] | None = None,
    cash_walk_start: float | None = None,
) -> list[EodBotOrder]:
    """Persist `fills` to eod_bot_orders."""
    if not fills:
        return []
    composite_scores = composite_scores or {}

    rows: list[EodBotOrder] = []
    cash = cash_walk_start
    with session_scope() as session:
        for f in fills:
            score = composite_scores.get(f.symbol)
            if cash is not None:
                cash_before = cash
                if f.side == "BUY":
                    cash_after = cash - f.fill_value - f.fee
                else:
                    cash_after = cash + f.fill_value - f.fee
                cash = cash_after
            else:
                cash_before = None
                cash_after = None

            order = EodBotOrder(
                bot_id=bot_id,
                spec_history_id=spec_history_id,
                run_id=run_id,
                side=f.side,
                symbol=f.symbol,
                decision_date=decision_date,
                fill_date=f.fill_date,
                quantity=f.quantity,
                fill_price=Decimal(str(round(f.fill_price, 2))),
                fill_value=Decimal(str(round(f.fill_value, 2))),
                fee=Decimal(str(round(f.fee, 2))),
                slippage_bps=Decimal(str(round(f.slippage_bps, 2))),
                composite_score=(
                    Decimal(str(round(score, 6))) if score is not None else None
                ),
                cash_before=(
                    Decimal(str(round(cash_before, 2)))
                    if cash_before is not None else None
                ),
                cash_after=(
                    Decimal(str(round(cash_after, 2)))
                    if cash_after is not None else None
                ),
                fill_source=f.fill_source,
            )
            session.add(order)
            rows.append(order)
        session.flush()
        return rows


def apply_orders_to_bot(
    *,
    bot_id: uuid.UUID,
    fills: list[FillPlan],
) -> tuple[Decimal, Decimal]:
    """Apply fill effects to bot.cash. Returns (new_cash, total_fees).

    Does NOT recompute holdings_value — that's done in
    `update_bot_state_after_tick`.
    """
    delta_cash = Decimal("0")
    total_fees = Decimal("0")
    for f in fills:
        fee_dec = Decimal(str(round(f.fee, 2)))
        val_dec = Decimal(str(round(f.fill_value, 2)))
        if f.side == "BUY":
            delta_cash = delta_cash - val_dec - fee_dec
        else:
            delta_cash = delta_cash + val_dec - fee_dec
        total_fees = total_fees + fee_dec

    with session_scope() as session:
        bot = _get_bot_for_update(session, bot_id)
        new_cash = bot.cash + delta_cash
        if new_cash < 0:
            raise RuntimeError(
                f"apply_orders_to_bot: bot {bot.name} would go negative "
                f"({bot.cash} + {delta_cash} = {new_cash})"
            )
        bot.cash = new_cash
        return (new_cash, total_fees)


def snapshot_positions(
    *,
    bot_id: uuid.UUID,
    decision_date: date,
    positions: list[dict[str, Any]],
) -> int:
    """UPSERT today's position snapshot."""
    if not positions:
        return 0
    sql = text("""
        INSERT INTO eod_bot_positions
            (bot_id, date, symbol, quantity, avg_cost,
             market_price, market_value, unrealized_pnl,
             weight_pct, sector, composite_score)
        VALUES
            (:bot, :d, :sym, :qty, :ac, :mp, :mv, :upnl,
             :wp, :sec, :cs)
        ON CONFLICT (bot_id, date, symbol) DO UPDATE SET
            quantity        = EXCLUDED.quantity,
            avg_cost        = EXCLUDED.avg_cost,
            market_price    = EXCLUDED.market_price,
            market_value    = EXCLUDED.market_value,
            unrealized_pnl  = EXCLUDED.unrealized_pnl,
            weight_pct      = EXCLUDED.weight_pct,
            sector          = EXCLUDED.sector,
            composite_score = EXCLUDED.composite_score
    """)
    inserted = 0
    with session_scope() as session:
        for p in positions:
            session.execute(sql, {
                "bot": bot_id,
                "d": decision_date,
                "sym": p["symbol"],
                "qty": int(p["quantity"]),
                "ac": Decimal(str(round(p["avg_cost"], 2))),
                "mp": (
                    Decimal(str(round(p["market_price"], 2)))
                    if p.get("market_price") is not None else None
                ),
                "mv": (
                    Decimal(str(round(p["market_value"], 2)))
                    if p.get("market_value") is not None else None
                ),
                "upnl": (
                    Decimal(str(round(p["unrealized_pnl"], 2)))
                    if p.get("unrealized_pnl") is not None else None
                ),
                "wp": (
                    Decimal(str(round(p["weight_pct"], 6)))
                    if p.get("weight_pct") is not None else None
                ),
                "sec": p.get("sector"),
                "cs": (
                    Decimal(str(round(p["composite_score"], 6)))
                    if p.get("composite_score") is not None else None
                ),
            })
            inserted += 1
        return inserted


def record_daily_pnl(
    *,
    bot_id: uuid.UUID,
    decision_date: date,
    cash: Decimal,
    holdings_value: Decimal,
    total_value: Decimal,
    seed_cash: Decimal,
    n_positions: int,
    trades_count: int,
    buy_value: Decimal,
    sell_value: Decimal,
    fee_total: Decimal,
) -> EodBotDailyPnL:
    """UPSERT today's PnL row, computing daily_return/cumulative/drawdown."""
    cum_ret = (
        (total_value - seed_cash) / seed_cash
        if seed_cash > 0 else None
    )

    sql_prev = text("""
        SELECT total_value, peak_total_value
          FROM eod_bot_daily_pnl
         WHERE bot_id = :bot AND date < :d
         ORDER BY date DESC LIMIT 1
    """)
    with get_engine().connect() as conn:
        prev = conn.execute(sql_prev, {"bot": bot_id, "d": decision_date}).first()

    if prev is None:
        daily_ret = None
        peak = total_value
    else:
        prev_tv = Decimal(str(prev[0]))
        prev_peak = Decimal(str(prev[1])) if prev[1] is not None else prev_tv
        daily_ret = (
            (total_value - prev_tv) / prev_tv if prev_tv > 0 else None
        )
        peak = max(prev_peak, total_value)

    drawdown = (
        (total_value - peak) / peak if peak > 0 else None
    )

    denom = (
        Decimal(str(prev[0])) if prev else seed_cash
    )
    turnover = (
        (buy_value + sell_value) / (denom * 2)
        if denom > 0 else None
    )

    sql_upsert = text("""
        INSERT INTO eod_bot_daily_pnl
            (bot_id, date, cash, holdings_value, total_value,
             daily_return_pct, cumulative_return_pct, drawdown_pct,
             peak_total_value, trades_count, buy_value, sell_value,
             fee_total, turnover_pct, n_positions)
        VALUES
            (:bot, :d, :c, :hv, :tv, :dr, :cr, :dd, :pk,
             :tc, :bv, :sv, :ft, :to, :np)
        ON CONFLICT (bot_id, date) DO UPDATE SET
            cash                  = EXCLUDED.cash,
            holdings_value        = EXCLUDED.holdings_value,
            total_value           = EXCLUDED.total_value,
            daily_return_pct      = EXCLUDED.daily_return_pct,
            cumulative_return_pct = EXCLUDED.cumulative_return_pct,
            drawdown_pct          = EXCLUDED.drawdown_pct,
            peak_total_value      = EXCLUDED.peak_total_value,
            trades_count          = EXCLUDED.trades_count,
            buy_value             = EXCLUDED.buy_value,
            sell_value            = EXCLUDED.sell_value,
            fee_total             = EXCLUDED.fee_total,
            turnover_pct          = EXCLUDED.turnover_pct,
            n_positions           = EXCLUDED.n_positions
    """)
    with session_scope() as session:
        session.execute(sql_upsert, {
            "bot": bot_id, "d": decision_date,
            "c": cash, "hv": holdings_value, "tv": total_value,
            "dr": daily_ret, "cr": cum_ret, "dd": drawdown, "pk": peak,
            "tc": trades_count, "bv": buy_value, "sv": sell_value,
            "ft": fee_total, "to": turnover, "np": n_positions,
        })

    return EodBotDailyPnL(
        bot_id=bot_id, date=decision_date, cash=cash,
        holdings_value=holdings_value, total_value=total_value,
        daily_return_pct=daily_ret, cumulative_return_pct=cum_ret,
        drawdown_pct=drawdown, peak_total_value=peak,
        trades_count=trades_count, buy_value=buy_value,
        sell_value=sell_value, fee_total=fee_total,
        turnover_pct=turnover, n_positions=n_positions,
    )


def update_bot_state_after_tick(
    *,
    bot_id: uuid.UUID,
    decision_date: date,
    last_tick_run_id: uuid.UUID,
    holdings_value: Decimal,
    total_value: Decimal,
) -> None:
    """Sync the bot's mirror columns after a tick is fully persisted."""
    with session_scope() as session:
        bot = _get_bot_for_update(session, bot_id)
        bot.holdings_value = holdings_value
        bot.total_value = total_value
        bot.last_tick_date = decision_date
        bot.last_tick_run_id = last_tick_run_id


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _get_bot_for_update(session: Session, bot_id: uuid.UUID) -> EodBot:
    """Row-level lock on the bot."""
    stmt = (
        select(EodBot)
        .where(EodBot.bot_id == bot_id)
        .with_for_update()
    )
    bot = session.execute(stmt).scalar_one_or_none()
    if bot is None:
        raise BotNotFoundError(f"bot_id {bot_id} not found")
    return bot
