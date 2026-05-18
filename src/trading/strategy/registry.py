"""Plugin strategy registry.

A plugin is a Python class that subclasses BaseStrategy and is decorated
with `@register_strategy("unique_name")`. The bot DB stores only the
unique name in `eod_bots.plugin_strategy_id`; the runner looks the name
up at execution time.
"""
from __future__ import annotations

from typing import Type

from src.trading.strategy.base import BaseStrategy

_REGISTRY: dict[str, Type[BaseStrategy]] = {}


def register_strategy(name: str):
    """Decorator: register a BaseStrategy subclass under `name`."""
    if not name or not name.replace("_", "").replace("-", "").isalnum():
        raise ValueError(
            f"register_strategy: invalid name '{name}' "
            "(alphanumeric/underscore/hyphen only)"
        )

    def _decorator(cls: Type[BaseStrategy]) -> Type[BaseStrategy]:
        if not issubclass(cls, BaseStrategy):
            raise TypeError(
                f"register_strategy: {cls.__name__} must subclass BaseStrategy"
            )
        if name in _REGISTRY and _REGISTRY[name] is not cls:
            raise ValueError(
                f"register_strategy: '{name}' already registered to "
                f"{_REGISTRY[name].__name__}"
            )
        _REGISTRY[name] = cls
        return cls

    return _decorator


def get_plugin_strategy(name: str) -> Type[BaseStrategy] | None:
    """Look up a registered plugin class. None if unknown."""
    _ensure_loaded()
    return _REGISTRY.get(name)


def list_plugin_strategies() -> list[str]:
    """All registered plugin strategy names, sorted."""
    _ensure_loaded()
    return sorted(_REGISTRY.keys())


_loaded = False


def _ensure_loaded() -> None:
    """Import strategies package on first access so decorators run."""
    global _loaded
    if _loaded:
        return
    try:
        import src.trading.strategy.strategies  # noqa: F401
    except ImportError:
        pass
    _loaded = True
