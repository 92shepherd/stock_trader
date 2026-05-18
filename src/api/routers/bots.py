"""EOD bot endpoints.

Endpoint map:
    POST   /bots                          - create new bot
    GET    /bots                          - list bots (filter by state)
    GET    /bots/{bot_id}                 - bot summary
    POST   /bots/{bot_id}/start           - PENDING -> RUNNING
    POST   /bots/{bot_id}/stop            - any -> STOPPED (영구)
    PATCH  /bots/{bot_id}/spec            - new spec version
    POST   /bots/{bot_id}/tick            - manual daily tick (debug)
    GET    /bots/{bot_id}/orders          - order history
    GET    /bots/{bot_id}/positions       - latest positions
    GET    /bots/{bot_id}/pnl             - daily PnL series
    GET    /bots/{bot_id}/runs            - run log
    GET    /bots/factors                  - factor catalog
    GET    /bots/strategies               - plugin strategy registry

가장 중요한 제약:
    KIS 매매 API 는 절대 호출하지 않는다. 매매/잔고/포지션은 전부 DB 안.
    STOPPED 봇은 다시 시작 불가능 (새 봇 생성으로 재시작).
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import text

from src.api.auth import require_api_key
from src.api.schemas import (
    DEFAULT_BOT_SEED_CASH,
    BotCreateRequest,
    BotListResponse,
    BotPatchSpecRequest,
    BotStopRequest,
    BotSummary,
    BotTickRequest,
    BotTickResponse,
    FactorInfo,
    StrategyInfo,
)
from src.db.connection import get_engine
from src.trading.bot import run_bot_daily
from src.trading.factors.catalog import all_factor_metadata
from src.trading.repositories.bots import (
    BotNameTakenError,
    BotNotFoundError,
    InvalidBotStateError,
    create_bot,
    get_bot,
    start_bot,
    stop_bot,
    upsert_spec_history,
)
from src.trading.strategy.declarative import StrategySpec
from src.trading.strategy.registry import (
    get_plugin_strategy,
    list_plugin_strategies,
)
from src.trading.strategy.validation import (
    SpecValidationError,
    required_factors_for_spec,
    validate_spec,
)
from src.utils.calendar import latest_business_day
from src.utils.logger import logger


router = APIRouter(
    prefix="/bots",
    tags=["bots"],
    dependencies=[Depends(require_api_key)],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_bot_id(bot_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(bot_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"bot_id": bot_id, "error": "not a valid UUID"},
        ) from e


def _summary_from_row(r: dict) -> BotSummary:
    """Convert a v_eod_bot_summary row to BotSummary response."""
    return BotSummary(
        bot_id=str(r["bot_id"]),
        name=r["name"],
        state=r["state"],
        strategy_kind=r["strategy_kind"],
        plugin_strategy_id=r.get("plugin_strategy_id"),
        universe=r["universe"],
        seed_cash=float(r["seed_cash"]),
        cash=float(r["cash"]),
        holdings_value=float(r["holdings_value"]),
        total_value=float(r["total_value"]),
        pnl=float(r["pnl"]) if r.get("pnl") is not None else 0.0,
        return_pct=(
            float(r["return_pct"]) if r.get("return_pct") is not None else None
        ),
        last_tick_date=r.get("last_tick_date"),
        created_at=r["created_at"].isoformat() if r.get("created_at") else "",
        started_at=(
            r["started_at"].isoformat() if r.get("started_at") else None
        ),
        stopped_at=(
            r["stopped_at"].isoformat() if r.get("stopped_at") else None
        ),
        final_pnl=(
            float(r["final_pnl"]) if r.get("final_pnl") is not None else None
        ),
        final_return_pct=(
            float(r["final_return_pct"])
            if r.get("final_return_pct") is not None else None
        ),
        total_orders=int(r.get("total_orders") or 0),
        current_spec_version=(
            int(r["current_spec_version"])
            if r.get("current_spec_version") is not None else None
        ),
    )


def _load_summary(bot_id: uuid.UUID) -> BotSummary:
    """Read v_eod_bot_summary for one bot."""
    sql = text("SELECT * FROM v_eod_bot_summary WHERE bot_id = :b")
    with get_engine().connect() as conn:
        row = conn.execute(sql, {"b": bot_id}).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"bot {bot_id} not found")
    return _summary_from_row(dict(row))


def _parse_and_validate_spec(spec_dict: dict) -> tuple[StrategySpec, list[str]]:
    """Parse the dict into StrategySpec + run cross-catalog validation.

    Returns (spec, required_factors). Raises HTTPException(422) on failure.
    """
    try:
        spec = StrategySpec.model_validate(spec_dict)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "declarative_spec shape invalid", "detail": str(e)},
        ) from e
    try:
        validate_spec(spec)
    except SpecValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "declarative_spec validation failed", "errors": e.errors},
        ) from e
    return spec, required_factors_for_spec(spec)


# ---------------------------------------------------------------------------
# Catalog endpoints (read-only, before path-param routes)
# ---------------------------------------------------------------------------


@router.get(
    "/factors",
    response_model=list[FactorInfo],
    summary="Factor catalog — 봇이 사용 가능한 모든 팩터",
)
async def get_factor_catalog() -> list[FactorInfo]:
    """Return every factor in the catalog with category + description.

    Used by the REST client when building a declarative spec — shows
    which factor names are valid for `signal.components.factor`.
    """
    return [
        FactorInfo(
            name=m.name,
            category=m.category,
            description=m.description,
            higher_is_better=m.higher_is_better,
        )
        for m in all_factor_metadata()
    ]


@router.get(
    "/strategies",
    response_model=list[StrategyInfo],
    summary="Plugin strategy registry — 등록된 모든 plugin 전략",
)
async def get_strategy_registry() -> list[StrategyInfo]:
    """List all registered plugin BaseStrategy implementations.

    Empty list is valid: declarative-only deployment.
    """
    out: list[StrategyInfo] = []
    for name in list_plugin_strategies():
        cls = get_plugin_strategy(name)
        if cls is None:
            continue
        out.append(StrategyInfo(
            name=name,
            class_name=cls.__name__,
            module=cls.__module__,
        ))
    return out


# ---------------------------------------------------------------------------
# Bot lifecycle
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=BotSummary,
    summary="새 EOD 봇 생성",
)
async def create_bot_endpoint(req: BotCreateRequest) -> BotSummary:
    """봇 생성 — PENDING 상태로 시작.

    `seed_cash` 가 None 이면 DEFAULT_BOT_SEED_CASH (1천만원) 사용.
    """
    # ---- Validate strategy_kind + body ----
    required_factors: list[str] | None = None
    if req.strategy_kind == "declarative":
        if not req.declarative_spec:
            raise HTTPException(
                status_code=422,
                detail="declarative_spec required for strategy_kind='declarative'",
            )
        _, required_factors = _parse_and_validate_spec(req.declarative_spec)
    else:  # plugin
        if not req.plugin_strategy_id:
            raise HTTPException(
                status_code=422,
                detail="plugin_strategy_id required for strategy_kind='plugin'",
            )
        if get_plugin_strategy(req.plugin_strategy_id) is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "plugin_strategy_id": req.plugin_strategy_id,
                    "error": "not registered",
                    "available": list_plugin_strategies(),
                },
            )

    # ---- Seed cash default ----
    seed_cash = Decimal(str(req.seed_cash if req.seed_cash is not None
                            else DEFAULT_BOT_SEED_CASH))

    try:
        bot = create_bot(
            name=req.name,
            strategy_kind=req.strategy_kind,
            universe=req.universe,
            seed_cash=seed_cash,
            declarative_spec=req.declarative_spec,
            plugin_strategy_id=req.plugin_strategy_id,
            required_factors=required_factors,
            notes=req.notes,
        )
    except BotNameTakenError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    logger.info(f"[bots] created bot {bot.name} ({bot.bot_id}) seed={seed_cash}")
    return _load_summary(bot.bot_id)


@router.get(
    "",
    response_model=BotListResponse,
    summary="봇 목록 조회",
)
async def list_bots_endpoint(
    state: str | None = Query(
        None, description="필터: PENDING / RUNNING / STOPPED. None = 전체."
    ),
    limit: int = Query(100, ge=1, le=1000),
) -> BotListResponse:
    """봇 목록 — v_eod_bot_summary 기반."""
    where = ""
    params: dict = {}
    if state is not None:
        if state not in ("PENDING", "RUNNING", "STOPPED"):
            raise HTTPException(
                status_code=422,
                detail=f"state must be PENDING/RUNNING/STOPPED, got '{state}'",
            )
        where = "WHERE state = :s"
        params["s"] = state
    params["limit"] = limit
    sql = text(
        f"SELECT * FROM v_eod_bot_summary {where} "
        "ORDER BY created_at DESC LIMIT :limit"
    )
    with get_engine().connect() as conn:
        rows = conn.execute(sql, params).mappings().all()
    return BotListResponse(bots=[_summary_from_row(dict(r)) for r in rows])


@router.get(
    "/{bot_id}",
    response_model=BotSummary,
    summary="봇 상세 조회",
)
async def get_bot_endpoint(bot_id: str) -> BotSummary:
    bid = _parse_bot_id(bot_id)
    return _load_summary(bid)


@router.post(
    "/{bot_id}/start",
    response_model=BotSummary,
    summary="봇 시작 (PENDING → RUNNING)",
)
async def start_bot_endpoint(bot_id: str) -> BotSummary:
    """PENDING 봇을 RUNNING 으로 전환. STOPPED 봇은 거부됨."""
    bid = _parse_bot_id(bot_id)
    try:
        start_bot(bid)
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except InvalidBotStateError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return _load_summary(bid)


@router.post(
    "/{bot_id}/stop",
    response_model=BotSummary,
    summary="봇 정지 (영구) — 이후 거래 없음",
)
async def stop_bot_endpoint(
    bot_id: str,
    req: BotStopRequest | None = None,
) -> BotSummary:
    """봇 정지. 정지 시점의 PnL / 누적수익률을 eod_bots 에 영구 기록.

    한 번 STOPPED 가 되면 어떤 daily tick 도 거래를 만들지 않는다 (불변).
    """
    bid = _parse_bot_id(bot_id)
    try:
        stop_bot(bid, reason=req.reason if req else None)
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _load_summary(bid)


@router.patch(
    "/{bot_id}/spec",
    response_model=BotSummary,
    summary="봇 spec 업데이트 (새 spec_version 추가)",
)
async def patch_spec_endpoint(
    bot_id: str, req: BotPatchSpecRequest,
) -> BotSummary:
    """spec 변경. STOPPED 봇은 거부됨.

    spec_history 에 새 row(version+1) 가 append-only 로 추가됨.
    """
    bid = _parse_bot_id(bot_id)

    required_factors: list[str] | None = None
    if req.strategy_kind == "declarative":
        if not req.declarative_spec:
            raise HTTPException(
                status_code=422,
                detail="declarative_spec required for strategy_kind='declarative'",
            )
        _, required_factors = _parse_and_validate_spec(req.declarative_spec)
    else:  # plugin
        if not req.plugin_strategy_id:
            raise HTTPException(
                status_code=422,
                detail="plugin_strategy_id required for strategy_kind='plugin'",
            )
        if get_plugin_strategy(req.plugin_strategy_id) is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "plugin_strategy_id": req.plugin_strategy_id,
                    "error": "not registered",
                },
            )

    try:
        upsert_spec_history(
            bot_id=bid,
            strategy_kind=req.strategy_kind,
            spec_json=req.declarative_spec,
            plugin_strategy_id=req.plugin_strategy_id,
            required_factors=required_factors,
            created_by="api",
            reason=req.reason,
        )
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except InvalidBotStateError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    return _load_summary(bid)


# ---------------------------------------------------------------------------
# Tick (debug / manual)
# ---------------------------------------------------------------------------


@router.post(
    "/{bot_id}/tick",
    response_model=BotTickResponse,
    summary="수동 daily tick (디버그/시뮬 용)",
)
async def manual_tick_endpoint(
    bot_id: str,
    req: BotTickRequest | None = Body(default=None),
) -> BotTickResponse:
    """봇을 즉시 한 번 tick 한다.

    프로덕션에서는 cron 이 매일 자동 실행하지만, 디버깅에는 수동 trigger
    가 유용하다. STOPPED 봇은 자동으로 SKIPPED 반환.

    Body 완전 생략 필드 허용: `-d ''`, body 없이 호출, `-d '{}'` 다 허용됨.
    모두 decision_date=None 으로 해석되어 latest_business_day() 이 대신 쓰임.
    """
    bid = _parse_bot_id(bot_id)
    try:
        get_bot(bid)
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    if req is None:
        req = BotTickRequest()

    dec = req.decision_date or latest_business_day()
    result = run_bot_daily(bid, dec)

    return BotTickResponse(
        bot_id=str(result.get("bot_id", bid)),
        decision_date=result.get("decision_date") or dec,
        status=result["status"],
        run_id=str(result["run_id"]) if result.get("run_id") else None,
        reason=result.get("reason"),
        n_buys=result.get("n_buys"),
        n_sells=result.get("n_sells"),
        total_value=result.get("total_value"),
        error=result.get("error"),
    )


# ---------------------------------------------------------------------------
# Read-only history queries
# ---------------------------------------------------------------------------


@router.get(
    "/{bot_id}/orders",
    summary="봇의 매수/매도 체결 기록 (최근 우선)",
)
async def get_orders_endpoint(
    bot_id: str,
    limit: int = Query(200, ge=1, le=5000),
    side: str | None = Query(None, description="BUY / SELL 필터."),
) -> dict:
    bid = _parse_bot_id(bot_id)
    where = ["bot_id = :b"]
    params: dict = {"b": bid, "limit": limit}
    if side is not None:
        if side not in ("BUY", "SELL"):
            raise HTTPException(
                status_code=422, detail=f"side must be BUY/SELL, got '{side}'"
            )
        where.append("side = :s")
        params["s"] = side
    sql = text(
        f"SELECT order_id, side, symbol, decision_date, fill_date, "
        f"       quantity, fill_price, fill_value, fee, slippage_bps, "
        f"       composite_score, cash_before, cash_after, run_id "
        f"  FROM eod_bot_orders "
        f"  WHERE {' AND '.join(where)} "
        f"  ORDER BY fill_date DESC, created_at DESC "
        f"  LIMIT :limit"
    )
    with get_engine().connect() as conn:
        rows = conn.execute(sql, params).mappings().all()
    return {
        "bot_id": bot_id,
        "count": len(rows),
        "orders": [
            {
                "order_id": str(r["order_id"]),
                "side": r["side"],
                "symbol": r["symbol"],
                "decision_date": r["decision_date"].isoformat(),
                "fill_date": r["fill_date"].isoformat(),
                "quantity": int(r["quantity"]),
                "fill_price": float(r["fill_price"]),
                "fill_value": float(r["fill_value"]),
                "fee": float(r["fee"]),
                "slippage_bps": float(r["slippage_bps"]),
                "composite_score": (
                    float(r["composite_score"])
                    if r["composite_score"] is not None else None
                ),
                "cash_before": (
                    float(r["cash_before"])
                    if r["cash_before"] is not None else None
                ),
                "cash_after": (
                    float(r["cash_after"])
                    if r["cash_after"] is not None else None
                ),
                "run_id": str(r["run_id"]) if r["run_id"] else None,
            }
            for r in rows
        ],
    }


@router.get(
    "/{bot_id}/positions",
    summary="봇의 현재 포지션 (가장 최근 스냅샷)",
)
async def get_positions_endpoint(bot_id: str) -> dict:
    bid = _parse_bot_id(bot_id)
    sql = text("""
        WITH latest_date AS (
            SELECT MAX(date) AS d
              FROM eod_bot_positions
             WHERE bot_id = :b
        )
        SELECT p.symbol, p.quantity, p.avg_cost, p.market_price,
               p.market_value, p.unrealized_pnl, p.weight_pct, p.sector,
               p.composite_score, p.date
          FROM eod_bot_positions p
          JOIN latest_date ld ON ld.d = p.date
         WHERE p.bot_id = :b AND p.quantity > 0
         ORDER BY p.market_value DESC NULLS LAST
    """)
    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"b": bid}).mappings().all()
    return {
        "bot_id": bot_id,
        "as_of": rows[0]["date"].isoformat() if rows else None,
        "count": len(rows),
        "positions": [
            {
                "symbol": r["symbol"],
                "quantity": int(r["quantity"]),
                "avg_cost": float(r["avg_cost"]),
                "market_price": (
                    float(r["market_price"])
                    if r["market_price"] is not None else None
                ),
                "market_value": (
                    float(r["market_value"])
                    if r["market_value"] is not None else None
                ),
                "unrealized_pnl": (
                    float(r["unrealized_pnl"])
                    if r["unrealized_pnl"] is not None else None
                ),
                "weight_pct": (
                    float(r["weight_pct"])
                    if r["weight_pct"] is not None else None
                ),
                "sector": r["sector"],
                "composite_score": (
                    float(r["composite_score"])
                    if r["composite_score"] is not None else None
                ),
            }
            for r in rows
        ],
    }


@router.get(
    "/{bot_id}/pnl",
    summary="봇의 일별 PnL / 누적수익률 시계열",
)
async def get_pnl_endpoint(
    bot_id: str,
    limit: int = Query(500, ge=1, le=5000),
) -> dict:
    bid = _parse_bot_id(bot_id)
    sql = text("""
        SELECT date, cash, holdings_value, total_value,
               daily_return_pct, cumulative_return_pct, drawdown_pct,
               peak_total_value, trades_count, buy_value, sell_value,
               fee_total, turnover_pct, n_positions
          FROM eod_bot_daily_pnl
         WHERE bot_id = :b
         ORDER BY date DESC
         LIMIT :limit
    """)
    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"b": bid, "limit": limit}).mappings().all()
    # ASC for charting
    rows = list(reversed(rows))
    return {
        "bot_id": bot_id,
        "count": len(rows),
        "pnl": [
            {
                "date": r["date"].isoformat(),
                "cash": float(r["cash"]),
                "holdings_value": float(r["holdings_value"]),
                "total_value": float(r["total_value"]),
                "daily_return_pct": (
                    float(r["daily_return_pct"])
                    if r["daily_return_pct"] is not None else None
                ),
                "cumulative_return_pct": (
                    float(r["cumulative_return_pct"])
                    if r["cumulative_return_pct"] is not None else None
                ),
                "drawdown_pct": (
                    float(r["drawdown_pct"])
                    if r["drawdown_pct"] is not None else None
                ),
                "peak_total_value": (
                    float(r["peak_total_value"])
                    if r["peak_total_value"] is not None else None
                ),
                "trades_count": int(r["trades_count"]),
                "buy_value": float(r["buy_value"]),
                "sell_value": float(r["sell_value"]),
                "fee_total": float(r["fee_total"]),
                "turnover_pct": (
                    float(r["turnover_pct"])
                    if r["turnover_pct"] is not None else None
                ),
                "n_positions": int(r["n_positions"]),
            }
            for r in rows
        ],
    }


@router.get(
    "/{bot_id}/runs",
    summary="봇의 daily tick 실행 로그",
)
async def get_runs_endpoint(
    bot_id: str,
    limit: int = Query(100, ge=1, le=1000),
) -> dict:
    bid = _parse_bot_id(bot_id)
    sql = text("""
        SELECT run_id, decision_date, status, started_at, finished_at,
               duration_ms, universe_size, eligible_size, scored_size,
               factor_coverage, target_size, n_buys, n_sells,
               skip_reason, error_message
          FROM eod_bot_runs
         WHERE bot_id = :b
         ORDER BY decision_date DESC
         LIMIT :limit
    """)
    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"b": bid, "limit": limit}).mappings().all()
    return {
        "bot_id": bot_id,
        "count": len(rows),
        "runs": [
            {
                "run_id": str(r["run_id"]),
                "decision_date": r["decision_date"].isoformat(),
                "status": r["status"],
                "started_at": (
                    r["started_at"].isoformat() if r["started_at"] else None
                ),
                "finished_at": (
                    r["finished_at"].isoformat() if r["finished_at"] else None
                ),
                "duration_ms": r["duration_ms"],
                "universe_size": r["universe_size"],
                "eligible_size": r["eligible_size"],
                "scored_size": r["scored_size"],
                "factor_coverage": r["factor_coverage"],
                "target_size": r["target_size"],
                "n_buys": int(r["n_buys"]),
                "n_sells": int(r["n_sells"]),
                "skip_reason": r["skip_reason"],
                "error_message": r["error_message"],
            }
            for r in rows
        ],
    }
