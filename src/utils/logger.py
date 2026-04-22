"""Logging setup using loguru."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

_CONFIGURED = False


def setup_logger() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_dir = Path(os.getenv("LOG_DIR", "./data/logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    level = os.getenv("LOG_LEVEL", "INFO").upper()

    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
               "<level>{message}</level>",
    )
    logger.add(
        log_dir / "stock_{time:YYYY-MM-DD}.log",
        level=level,
        rotation="00:00",
        retention="30 days",
        encoding="utf-8",
    )

    _CONFIGURED = True


setup_logger()

__all__ = ["logger", "setup_logger"]
