"""Strategy layer for EOD bots.

Two flavors of strategy implementation:
    DeclarativeStrategy  - driven by a JSON spec (`StrategySpec`).
                           Created via REST API. Cannot run arbitrary
                           Python code.
    Plugin strategies    - subclass `BaseStrategy` and register via
                           `@register_strategy("name_v1")`. Selected
                           by setting eod_bots.plugin_strategy_id.

Both flavors implement the same `BaseStrategy` interface so the bot
engine doesn't care which kind it's running.
"""
from __future__ import annotations

from src.trading.strategy.base import BaseStrategy
from src.trading.strategy.declarative import (
    DeclarativeStrategy,
    ExecutionSpec,
    StrategySpec,
)
from src.trading.strategy.registry import (
    get_plugin_strategy,
    list_plugin_strategies,
    register_strategy,
)
from src.trading.strategy.validation import (
    SpecValidationError,
    required_factors_for_spec,
    validate_spec,
)

__all__ = [
    "BaseStrategy",
    "DeclarativeStrategy",
    "ExecutionSpec",
    "SpecValidationError",
    "StrategySpec",
    "get_plugin_strategy",
    "list_plugin_strategies",
    "register_strategy",
    "required_factors_for_spec",
    "validate_spec",
]
