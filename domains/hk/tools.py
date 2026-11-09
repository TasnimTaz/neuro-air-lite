"""Runtime injection and tools for Hong Kong domain agents.

Injects:
  engine: SQLAlchemy Engine connected with read-only 'neuro_ro' role and hard statement timeout.
  pd:     pandas module.
  np:     numpy module.
  text:   sqlalchemy.text helper for writing SQL queries.

Strict Isolation:
  DB_URL_ADMIN and the admin engine are NEVER injected or exposed to this
  namespace, and `load_agent_env()` removes the admin DSN from `os.environ` so a
  module that already holds `os` (pandas does) cannot read it either.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import text

from core.env import load_agent_env
from core.runtime import PythonRuntime
from domains.hk.db import make_ro_engine


def build_runtime(statement_timeout_ms: int | None = None) -> PythonRuntime:
    """Build a fresh PythonRuntime wired with the read-only database engine."""
    load_agent_env()
    engine = make_ro_engine(statement_timeout_ms=statement_timeout_ms)
    variables = {
        "engine": engine,
        "pd": pd,
        "np": np,
        "text": text,
    }
    return PythonRuntime(variables=variables)
