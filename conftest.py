"""Pytest bootstrap.

The suite loads the *agent* environment, not the full `.env`: only `DB_URL_RO`
and the `LLM_*` keys are exported, and `DB_URL_ADMIN` is scrubbed. That is what
production does (`core.env.load_agent_env()`), so a test that accidentally
relies on the admin DSN shows up here instead of in a live run.

DB-dependent tests skip themselves when the engine cannot connect; the pure unit
tests always run, which keeps `pytest` useful on a machine without Docker.
"""

from __future__ import annotations

from core.env import load_agent_env

load_agent_env(override=True, scrub=True)
