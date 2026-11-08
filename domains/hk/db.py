"""Database engines for the Hong Kong domain.

Two profiles, never interchangeable:

    make_ro_engine()     the agent. neuro_ro role, read only, 5 s statement timeout.
    make_admin_engine()  ingestion and truth_sql. Full privileges, not sandboxed.

The agent runtime only ever receives the read-only engine; DB_URL_ADMIN is never
injected into the Python namespace.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

DEFAULT_STATEMENT_TIMEOUT_MS = 5000


def _timeout_ms() -> int:
    raw = os.environ.get("STATEMENT_TIMEOUT_MS", str(DEFAULT_STATEMENT_TIMEOUT_MS))
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_STATEMENT_TIMEOUT_MS


def make_ro_engine(
    url: str | None = None,
    statement_timeout_ms: int | None = None,
) -> Engine:
    """Engine for the agent runtime: read-only role with a hard statement timeout."""
    timeout = statement_timeout_ms or _timeout_ms()
    return create_engine(
        url or os.environ["DB_URL_RO"],
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 5,
            # Server-side cap. The role also sets it via ALTER ROLE, but passing it
            # here means a role default changed behind our back cannot lift it.
            "options": f"-c statement_timeout={timeout}",
        },
    )


def make_admin_engine(url: str | None = None) -> Engine:
    """Engine for ingestion and truth queries. Never give this to the model."""
    return create_engine(
        url or os.environ["DB_URL_ADMIN"],
        pool_size=2,
        max_overflow=4,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10},
    )
