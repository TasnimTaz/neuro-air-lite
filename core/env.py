"""Environment loading with least privilege for agent processes.

`ingest.common.load_env()` exports *every* key in `.env` into `os.environ`, which
is correct for ingest scripts and wrong for the agent. The runtime's AST checker
can always be bypassed by a module that already holds a reference to `os`
(pandas does: `pd.io.common.os`), so an exported `DB_URL_ADMIN` is reachable from
model-written code no matter how long the blacklist gets.

This module is the agent-side loader:

* `load_agent_env()` copies only the keys an agent legitimately needs, and scrubs
  the admin credentials from `os.environ` if the parent shell exported them.
* `admin_url()` returns the admin DSN to the *caller* without exporting it, so
  only `scripts/check_agent.py`'s truth queries ever see it.

The real isolation boundary is the Day 10 subprocess with a scrubbed environ.
This is defence in depth until then, and it is what makes the "the agent cannot
read the admin password" claim true rather than aspirational.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]

# Keys the runtime and the LLM client read from os.environ.
AGENT_EXACT_KEYS = frozenset(
    {
        "DB_URL_RO",
        "STATEMENT_TIMEOUT_MS",
        "MAX_TOTAL_STEPS",
        "SYNTH_RESERVE",
        "VERIFY_RESERVE",
    }
)
AGENT_KEY_PREFIXES = ("LLM_",)

# Never imported into an agent process. Scrubbed rather than merely not loaded.
ADMIN_ONLY_KEYS = frozenset(
    {
        "DB_URL_ADMIN",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
        "POSTGRES_DB",
    }
)


@lru_cache(maxsize=1)
def _file_values() -> dict[str, str]:
    """Parse `.env` without touching os.environ. Cached for the process lifetime."""
    path = ROOT / ".env"
    if not path.exists():
        return {}
    return {key: value for key, value in dotenv_values(path).items() if value is not None}


def _is_agent_key(key: str) -> bool:
    return key in AGENT_EXACT_KEYS or any(key.startswith(prefix) for prefix in AGENT_KEY_PREFIXES)


def load_agent_env(*, override: bool = False, scrub: bool = True) -> None:
    """Export the agent's own configuration, and nothing else.

    Args:
        override: overwrite keys that are already in `os.environ`. The default is
            to let an explicit shell value win, which is how operators test a
            different model or DSN without editing `.env`.
        scrub: remove admin-only keys from `os.environ`. On by default; the only
            caller that turns it off is an admin script that wants the old
            behaviour, and those use `ingest.common.load_env()` instead.
    """
    if scrub:
        for key in ADMIN_ONLY_KEYS:
            os.environ.pop(key, None)

    for key, value in _file_values().items():
        if not _is_agent_key(key):
            continue
        if override or key not in os.environ:
            os.environ[key] = value


def admin_url() -> str:
    """Return the admin DSN without exporting it.

    Reads `.env` directly rather than `os.environ` first, because
    `load_agent_env()` deliberately removed it. Kept for `truth_sql` in the
    acceptance checks, which run outside the sandbox on the admin engine.
    """
    value = _file_values().get("DB_URL_ADMIN") or os.environ.get("DB_URL_ADMIN")
    if not value:
        raise RuntimeError("DB_URL_ADMIN is not set in .env; the truth queries need it")
    return value
