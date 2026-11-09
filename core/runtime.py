"""In-process Python execution runtime and AST security checker for Neuro-Air Lite.

Architecture Note:
    This runtime uses Python's in-process `exec()` with a shared dictionary namespace.
    It provides logical isolation and namespace persistence for worker agents, but
    it is NOT an OS-level sandbox. Furthermore, `contextlib.redirect_stdout` is process-wide,
    so concurrent requests in the same process are unsafe until Day 10 (which migrates
    execution to an isolated subprocess / RPC model).

Security Model:
    Before execution, every code snippet is parsed into an AST and checked against:
    - Forbidden imports (e.g., os, sys, subprocess, requests, urllib, etc.)
    - Forbidden builtins/calls (e.g., eval, exec, compile, open, getattr, setattr, globals, locals, vars, etc.)
    - Forbidden names, including `__builtins__` (which would otherwise reach eval)
    - Forbidden attributes: `os`, `sys`, `environ`, `system`, `popen`, ... reachable
      through already-imported modules (e.g. `pd.io.common.os.environ`)
    - Dunder attribute access (e.g., __class__, __subclasses__, __dict__, etc.)
    - Dangerous SQL statements in string constants and f-string literals.

    A blacklist is never complete: `'DR' + 'OP TABLE x'` still gets through, which is
    why the database role is read-only and why the admin DSN is not exported into
    the process environment at all (see `core/env.py`). The AST layer is a guard
    rail for an honest model, not the security boundary.
"""

from __future__ import annotations

import ast
import contextlib
import io
import re
import time
import traceback
import types
from dataclasses import dataclass
from typing import Any

FORBIDDEN_IMPORTS = {
    "os",
    "sys",
    "subprocess",
    "shutil",
    "socket",
    "requests",
    "urllib",
    "pathlib",
    "ctypes",
    "multiprocessing",
    "importlib",
    "builtins",
}

FORBIDDEN_CALLS = {
    "eval",
    "exec",
    "compile",
    "open",
    "__import__",
    "input",
    "exit",
    "quit",
    "getattr",
    "setattr",
    "globals",
    "locals",
    "vars",
    "breakpoint",
}

# Names that are dangerous even when merely referenced, not called.
FORBIDDEN_NAMES = FORBIDDEN_CALLS | {
    "__builtins__",
    "__loader__",
    "__spec__",
}

# Attribute names that expose the process environment through an object the
# sandbox legitimately hands the model (pandas and numpy both import `os`).
FORBIDDEN_ATTRS = {
    "os",
    "sys",
    "environ",
    "environb",
    "getenv",
    "putenv",
    "system",
    "popen",
    "fork",
    "execv",
    "execve",
    "execl",
    "spawn",
    "spawnl",
    "spawnv",
    "kill",
}

# Write / DDL / session SQL anywhere in a statement, not only at position 0.
# The old `^\s*(insert|update|...)\b` guard blocked a harmless label such as
# `print('Set of stations:', 3)` and let `SELECT 1; SET statement_timeout = 0`
# through. This pattern anchors on a statement boundary (`^` or `;`), allows
# leading comments, and requires the object the verb acts on.
SQL_WRITE_PATTERN = re.compile(
    r"(?:^|;)\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*"
    r"(?:insert\s+into\b|update\s+[\w.\"]+\s+set\b|delete\s+from\b"
    r"|(?:drop|create|alter)\s+(?:table|index|view|schema|database|type|role|user|extension|function|sequence)\b"
    r"|truncate\s+(?:table\s+)?[\w.\"]+|grant\s+\w+\s+on\b|revoke\s+\w+\s+on\b"
    r"|set\s+(?:session|local)\b|set\s+\w+\s*(?:=|\s+to\s)|reset\s+\w+\s*;?\s*$"
    r"|copy\s+[\w.\"(]+\s+(?:to|from)\b|call\s+\w+\s*\(|do\s+\$|lock\s+table\b|vacuum\b)",
    re.IGNORECASE | re.DOTALL,
)


class SecurityError(Exception):
    """Raised when submitted code violates runtime AST security rules."""


def _check_sql(text: str, *, source: str) -> None:
    match = SQL_WRITE_PATTERN.search(text)
    if match:
        snippet = text[match.start() : match.start() + 60].strip()
        raise SecurityError(f"write/DDL/session SQL in {source} blocked: {snippet}...")


def check_code(src: str) -> None:
    """Validate python code using AST inspection before execution."""
    tree = ast.parse(src)

    for n in ast.walk(tree):
        # 1. Imports
        if isinstance(n, ast.Import):
            for a in n.names:
                root_pkg = a.name.split(".")[0]
                if root_pkg in FORBIDDEN_IMPORTS:
                    raise SecurityError(f"forbidden import: {a.name}")
        elif isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            root_pkg = mod.split(".")[0]
            if root_pkg in FORBIDDEN_IMPORTS:
                raise SecurityError(f"forbidden import: {n.module}")

        # 2. Function calls
        elif isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name) and n.func.id in FORBIDDEN_CALLS:
                raise SecurityError(f"forbidden call: {n.func.id}()")
            if isinstance(n.func, ast.Attribute) and n.func.attr in FORBIDDEN_ATTRS:
                raise SecurityError(f"forbidden call: .{n.func.attr}()")

        # 3. Bare names, including `__builtins__['eval']`
        elif isinstance(n, ast.Name):
            if n.id in FORBIDDEN_NAMES:
                raise SecurityError(f"forbidden name: {n.id}")

        # 4. Attribute access
        elif isinstance(n, ast.Attribute):
            if n.attr.startswith("__"):
                raise SecurityError(f"dunder access blocked: {n.attr}")
            if n.attr in FORBIDDEN_ATTRS:
                raise SecurityError(f"forbidden attribute: .{n.attr}")

        # 5. String constants with SQL commands
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            _check_sql(n.value, source="string constant")

        # 6. JoinedStr (f-strings): check the literals joined together, which
        #    catches `f'INSERT INTO {tbl} VALUES (1)'` split into pieces.
        elif isinstance(n, ast.JoinedStr):
            joined = "".join(
                part.value for part in n.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            _check_sql(joined, source="f-string")


@dataclass
class ExecResult:
    ok: bool
    stdout: str
    error: str = ""
    elapsed_seconds: float = 0.0
    first_attempt: bool = True

    def render(self, limit: int = 10000) -> str:
        text = self.stdout
        if self.error:
            text = text + ("\n" if text else "") + "ERROR:\n" + self.error
        text = text.strip() or "(no output)"
        if len(text) <= limit:
            return text
        return text[:limit] + "\n... [truncated]"


class PythonRuntime:
    """Persistent shared namespace for worker agents."""

    def __init__(self, variables: dict[str, Any] | None = None):
        self.ns: dict[str, Any] = {"__name__": "__sandbox__"}
        self.ns.update(variables or {})
        self._injected = set(self.ns)

    def execute(self, code: str, first_attempt: bool = True) -> ExecResult:
        started = time.perf_counter()
        buf = io.StringIO()
        try:
            check_code(code)
            with contextlib.redirect_stdout(buf):
                exec(compile(code, "<agent>", "exec"), self.ns)
            elapsed = time.perf_counter() - started
            return ExecResult(
                ok=True,
                stdout=buf.getvalue(),
                elapsed_seconds=elapsed,
                first_attempt=first_attempt,
            )
        except SecurityError as e:
            elapsed = time.perf_counter() - started
            return ExecResult(
                ok=False,
                stdout=buf.getvalue(),
                error=f"SecurityError: {e}",
                elapsed_seconds=elapsed,
                first_attempt=first_attempt,
            )
        except Exception:
            elapsed = time.perf_counter() - started
            return ExecResult(
                ok=False,
                stdout=buf.getvalue(),
                error=traceback.format_exc(limit=3),
                elapsed_seconds=elapsed,
                first_attempt=first_attempt,
            )

    def close(self) -> None:
        """Dispose the injected engine, if any.

        `build_runtime()` creates a connection pool per worker; without this the
        pool is garbage-collected but not closed deterministically, which leaks
        Postgres backends across a long evaluation run.
        """
        for value in self.ns.values():
            dispose = getattr(value, "dispose", None)
            if callable(dispose) and value is not None and hasattr(value, "pool"):
                dispose()

    def __enter__(self) -> "PythonRuntime":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def manifest(self) -> dict[str, str]:
        """Describe variables currently staged in the shared namespace."""
        out = {}
        for k, v in self.ns.items():
            if (
                k.startswith("_")
                or k in self._injected
                or callable(v)
                or isinstance(v, types.ModuleType)
            ):
                continue
            shape = getattr(v, "shape", None)
            shape_str = f" {tuple(shape)}" if shape else ""
            out[k] = f"{type(v).__name__}{shape_str}"
        return out
