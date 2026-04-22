"""Database connections.

We use two paths:
    - SQLAlchemy engine for general ORM queries
    - Raw psycopg3 connection for bulk COPY
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.config import get_db_settings

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _SessionFactory
    if _engine is None:
        settings = get_db_settings()
        _engine = create_engine(
            settings.sync_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            future=True,
        )
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Managed SQLAlchemy session with commit/rollback."""
    get_engine()
    assert _SessionFactory is not None
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def raw_connection() -> Iterator[psycopg.Connection]:
    """Raw psycopg3 connection for COPY operations."""
    settings = get_db_settings()
    conn = psycopg.connect(settings.psycopg_dsn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
