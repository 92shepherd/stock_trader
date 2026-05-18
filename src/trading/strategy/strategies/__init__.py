"""Plugin strategy implementations.

Each module here defines one or more BaseStrategy subclasses decorated
with `@register_strategy("name_vN")`. They are auto-discovered when
this package is first imported.
"""
from __future__ import annotations
