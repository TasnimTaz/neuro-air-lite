"""Environment loading with least privilege for agent processes.

`ingest.common.load_env()` exports the repo `.env` *and* the admin-only file into
`os.environ`, which is correct for ingest scripts and wrong for the agent. The
runtime's AST checker can always be bypassed by a module that already holds a
reference to `os` (pandas does: `pd.io.common.os`), so an exported
`DB_URL_ADMIN` is reachable from model-written code no matter how long the
blacklist gets.

The admin credentials live outside the repository, in
`~/.neuro-air/admin.env` by default (`NEURO_ADMIN_ENV` overrides the path). The
repo's `.env` contains only the read-only DSN and LLM settings, so an accidental
or API-level read of `.env` (pandas, numpy, a shell-out) cannot expose the admin
password.

This module is the agent-side loader:

* `load_agent_env()` copies only the keys an agent legitimately needs, and scrubs
  the admin credentials from `os.environ` if the parent shell exported them.
* `admin_url()` returns the admin DSN to the *caller* without exporting it, so
  only `scripts/check_day4.py`'s truth queries ever see it.
* `load_admin_env()` is the host-side loader for ingest and schema scripts.

The real isolation boundary is the Day 10 subprocess with a scrubbed environ.
Until then a *deliberate absolute-path read* of `~/.neuro-air/admin.env` from
model code is still possible; accidental and ordinary API-level reads are not.
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
        "NEURO_ADMIN_ENV",
    }
)

ADMIN_ENV_KEYS = frozenset(
    {
        "DB_URL_ADMIN",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
        "POSTGRES_DB",
    }
)


@lru_cache(maxsize=1)
def _file_values() -> dict[str, str]:
    """Parse the repo `.env` without touching os.environ.

    Cached for the process lifetime. This file is deliberately model-readable:
    it must never contain an admin-only key (the test suite enforces this).
    """
    path = ROOT / ".env"
    if not path.exists():
        return {}
    return {key: value for key, value in dotenv_values(path).items() if value is not None}


def admin_env_path() -> Path:
    """Return the admin-only env file path, outside the repository."""
    override = os.environ.get("NEURO_ADMIN_ENV")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".neuro-air" / "admin.env"


def _admin_values() -> dict[str, str]:
    """Parse the admin-only file without exporting it.

    No cache: `admin_url()` is called only by host-side truth checks, and tests
    change `NEURO_ADMIN_ENV` between calls.
    """
    path = admin_env_path()
    if not path.exists():
        return {}
    values = dotenv_values(path)
    return {key: value for key, value in values.items() if value is not None}


def _is_agent_key(key: str) -> bool:
    return key in AGENT_EXACT_KEYS or any(key.startswith(prefix) for prefix in AGENT_KEY_PREFIXES)


def load_admin_env(*, override: bool = True) -> Path:
    """Export the admin-only file into `os.environ` for host-side scripts.

    Raises a clear error instead of letting a later `KeyError` fail at the
    database connection, where the cause is much harder to see.
    """
    from dotenv import load_dotenv

    path = admin_env_path()
    if not path.exists():
        raise RuntimeError(
            f"admin credentials not found at {path}. Create it with "
            "POSTGRES_* and DB_URL_ADMIN, or set NEURO_ADMIN_ENV to its path. "
            "Never put these keys in the repo .env."
        )
    load_dotenv(path, override=override)
    return path


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

    Reads the admin-only file outside the repo, so a model that can read the
    repo `.env` still cannot see this value. An explicit shell variable wins
    when present; `load_agent_env()` scrubs it from agent processes.
    """
    value = os.environ.get("DB_URL_ADMIN") or _admin_values().get("DB_URL_ADMIN")
    if not value:
        raise RuntimeError(
            "DB_URL_ADMIN is not set. Put it in the admin-only file "
            f"{admin_env_path()} (override with NEURO_ADMIN_ENV), never in the repo .env."
        )
    return value
