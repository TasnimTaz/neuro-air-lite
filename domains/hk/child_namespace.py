"""Namespace builder for the Day 10 subprocess runtime.

The engine is created *inside* the child process: SQLAlchemy engines are not
picklable, and handing a parent engine to a child would also defeat the point of
the scrubbed environment. This module deliberately does not call
``load_agent_env()``; the parent passes only the keys the child needs.

``domains.hk.tools`` is imported for ``geocode`` and the chart manager, but its
``build_runtime()`` is not used here because that helper loads the repo ``.env``
into the child environment.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text

from domains.hk.db import make_ro_engine
from domains.hk.tools import geocode
from domains.hk.viz import EChartsConfigManager


def build_child_namespace(statement_timeout_ms: int | None = None) -> dict[str, Any]:
    """Build a fresh read-only namespace in the child process."""
    engine = make_ro_engine(statement_timeout_ms=statement_timeout_ms)
    return {
        "__name__": "__sandbox__",
        "engine": engine,
        "pd": pd,
        "np": np,
        "text": text,
        "geocode": geocode,
        "charts": EChartsConfigManager(),
    }
