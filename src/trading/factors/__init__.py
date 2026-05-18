"""Factor catalog and panel loader for EOD bots.

This subpackage is the bot's gateway to factor data. Bots never touch
factor_signals directly — they go through `load_factor_panel()` so the
SQL shape and column conventions stay in one place.
"""
from __future__ import annotations

from src.trading.factors.catalog import (
    FACTOR_CATEGORIES,
    FactorMetadata,
    all_factor_metadata,
    get_factor_metadata,
    is_known_factor,
    list_factor_names,
)
from src.trading.factors.loader import factor_coverage, load_factor_panel

__all__ = [
    "FACTOR_CATEGORIES",
    "FactorMetadata",
    "all_factor_metadata",
    "factor_coverage",
    "get_factor_metadata",
    "is_known_factor",
    "list_factor_names",
    "load_factor_panel",
]
