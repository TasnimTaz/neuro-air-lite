"""Spawned subprocess runtime with an RPC namespace proxy.

This is the Day 10 execution layer. The in-process runtime in ``core.runtime``
is still the evaluation default and is byte-for-byte unchanged; this module adds
an alternative used by the API when ``RUNTIME_MODE=subprocess``.

What it isolates: model-written code executes in a child process with a scrubbed
environment and a fresh empty working directory. The child receives only the
read-only DSN plus the minimum OS variables, creates its own SQLAlchemy engine,
captures its own stdout, and answers over a Pipe. A wall-clock timeout kills the
child; the next execute starts a fresh child and staged state is lost.

What it does NOT isolate: the child runs as the same OS user, can still reach the
Postgres network endpoint, and is not a container sandbox. The AST checker is
still only a guard rail; the read-only DB role is the real data boundary.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import multiprocessing
import os
import shutil
import tempfile
import threading
import time
import traceback
import types
from pathlib import Path
from typing import Any

from core.runtime import ExecResult, SecurityError, check_code

DEFAULT_BUILDER = "domains.hk.child_namespace:build_child_namespace"
ROOT = Path(__file__).resolve().parents[1]
MAX_STDOUT_CHARS = 20_000
READY_TIMEOUT_SECONDS = 30.0
RPC_TIMEOUT_SECONDS = 60.0

# Windows needs these for the interpreter and TLS to work. Deliberately absent:
# DB_URL_ADMIN, POSTGRES_*, NEURO_ADMIN_ENV, LLM_*, EMBED_*.
OS_ENV_KEYS = (
    "SystemRoot",
    "TMP",
    "TEMP",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "COMSPEC",
    "PATHEXT",
    "PATH",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)

_ENV_START_LOCK = threading.Lock()
_MISSING = object()


def _truncate(text: str) -> str:
    if len(text) <= MAX_STDOUT_CHARS:
        return text
    return text[:MAX_STDOUT_CHARS] + "\n... [truncated]"


def _child_entry(
    builder_ref: str,
    conn: Any,
    statement_timeout_ms: int | None,
    tempdir: str,
) -> None:
    """Child process entry point. Never calls the parent-side AST checker."""
    try:
        os.chdir(tempdir)
        module_name, attr = builder_ref.split(":", 1)
        builder = getattr(importlib.import_module(module_name), attr)
        ns: dict[str, Any] = builder(statement_timeout_ms=statement_timeout_ms)
        injected = set(ns)
        conn.send({"ok": True})
    except BaseException:  # pragma: no cover - startup failure is surfaced in parent
        conn.send({"ok": False, "error": traceback.format_exc()})
        conn.close()
        return

    while True:
        try:
            message = conn.recv()
        except (EOFError, OSError):
            break
        op = message.get("op")

        if op == "close":
            engine = ns.get("engine")
            dispose = getattr(engine, "dispose", None)
            if callable(dispose):
                dispose()
            conn.send({"ok": True})
            break

        if op == "execute":
            started = time.perf_counter()
            buf = io.StringIO()
            ok = True
            error = ""
            try:
                with contextlib.redirect_stdout(buf):
                    exec(compile(message["code"], "<agent>", "exec"), ns)
            except BaseException:
                ok = False
                error = traceback.format_exc(limit=3)
            conn.send(
                {
                    "ok": True,
                    "result": ExecResult(
                        ok=ok,
                        stdout=_truncate(buf.getvalue()),
                        error=error,
                        elapsed_seconds=time.perf_counter() - started,
                        first_attempt=message.get("first_attempt", True),
                    ),
                }
            )
            continue

        if op == "manifest":
            out: dict[str, str] = {}
            for key, value in ns.items():
                if (
                    key.startswith("_")
                    or key in injected
                    or callable(value)
                    or isinstance(value, types.ModuleType)
                ):
                    continue
                shape = getattr(value, "shape", None)
                shape_str = f" {tuple(shape)}" if shape else ""
                out[key] = f"{type(value).__name__}{shape_str}"
            conn.send({"ok": True, "result": out})
            continue

        if op in ("ns_get", "ns_getitem"):
            key = message["key"]
            # Engines are not picklable. The parent wraps this key in a proxy.
            value = None if key == "engine" else ns.get(key, _MISSING)
            exists = key == "engine" or value is not _MISSING
            if op == "ns_getitem" and not exists:
                conn.send({"ok": True, "exists": False})
            else:
                conn.send({"ok": True, "exists": exists, "value": None if not exists else value})
            continue

        if op == "ns_contains":
            conn.send({"ok": True, "result": message["key"] in ns})
            continue

        if op == "ns_set":
            ns[message["key"]] = message["value"]
            conn.send({"ok": True})
            continue

        if op == "ns_pop":
            key = message["key"]
            if key in ns:
                conn.send({"ok": True, "exists": True, "value": ns.pop(key)})
            elif message.get("has_default"):
                conn.send({"ok": True, "exists": False, "value": message.get("default")})
            else:
                conn.send({"ok": True, "exists": False})
            continue

        if op == "engine_execute":
            try:
                sql = message["sql"]
                with ns["engine"].connect() as connection:
                    rows = connection.execute(ns["text"](sql), message.get("params") or {}).fetchall()
                conn.send({"ok": True, "rows": [tuple(row) for row in rows]})
            except BaseException:
                conn.send({"ok": False, "error": traceback.format_exc(limit=3)})
            continue

        if op == "engine_url":
            conn.send({"ok": True, "result": str(ns["engine"].url)})
            continue

        if op == "env_keys":
            conn.send({"ok": True, "result": sorted(os.environ)})
            continue

        if op == "cwd_probe":
            conn.send(
                {
                    "ok": True,
                    "result": {"cwd": os.getcwd(), "env_exists": os.path.exists(".env")},
                }
            )
            continue

        conn.send({"ok": False, "error": f"unknown op: {op}"})

    conn.close()


class RPCError(RuntimeError):
    """The child process failed or disconnected."""


class ChildTimeout(RPCError):
    """A child operation exceeded its wall-clock budget."""


class _ResultProxy:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _ConnectionProxy:
    def __init__(self, ns: "_NamespaceProxy") -> None:
        self._ns = ns

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _ResultProxy:
        sql = statement.text if hasattr(statement, "text") else str(statement)
        response = self._ns._rpc(
            {"op": "engine_execute", "sql": sql, "params": params or {}},
            timeout=RPC_TIMEOUT_SECONDS,
        )
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "engine execute failed"))
        return _ResultProxy(response["rows"])

    def __enter__(self) -> "_ConnectionProxy":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class _EngineProxy:
    """Enough of SQLAlchemy's Engine surface for host-side prompt building."""

    is_subprocess_proxy = True

    def __init__(self, ns: "_NamespaceProxy") -> None:
        self._ns = ns

    def connect(self) -> _ConnectionProxy:
        return _ConnectionProxy(self._ns)

    @property
    def url(self) -> str:
        response = self._ns._rpc({"op": "engine_url"}, timeout=RPC_TIMEOUT_SECONDS)
        return str(response["result"])

    def dispose(self) -> None:
        # The child disposes the real engine when it closes.
        return None


class _NamespaceProxy:
    """Read/write proxy for the child namespace, used by the host harness."""

    def __init__(self, runtime: "SubprocessRuntime") -> None:
        self._runtime = runtime

    def _rpc(self, message: dict[str, Any], timeout: float = RPC_TIMEOUT_SECONDS) -> dict[str, Any]:
        return self._runtime._rpc(message, timeout=timeout)

    def _wrap(self, key: str, value: Any) -> Any:
        return _EngineProxy(self) if key == "engine" else value

    def get(self, key: str, default: Any = None) -> Any:
        response = self._rpc(
            {"op": "ns_get", "key": key, "default": default, "has_default": True}
        )
        if not response["exists"]:
            return default
        return self._wrap(key, response["value"])

    def __getitem__(self, key: str) -> Any:
        response = self._rpc({"op": "ns_getitem", "key": key})
        if not response["exists"]:
            raise KeyError(key)
        return self._wrap(key, response["value"])

    def __contains__(self, key: object) -> bool:
        return bool(self._rpc({"op": "ns_contains", "key": key})["result"])

    def __setitem__(self, key: str, value: Any) -> None:
        self._rpc({"op": "ns_set", "key": key, "value": value})

    def pop(self, key: str, default: Any = _MISSING) -> Any:
        message: dict[str, Any] = {"op": "ns_pop", "key": key}
        if default is not _MISSING:
            message.update({"has_default": True, "default": default})
        response = self._rpc(message)
        if response["exists"]:
            return response["value"]
        if default is not _MISSING:
            return default
        raise KeyError(key)


class SubprocessRuntime:
    """Spawn-backed runtime with the same public surface as ``PythonRuntime``."""

    is_subprocess = True

    def __init__(
        self,
        *,
        builder_ref: str = DEFAULT_BUILDER,
        execute_timeout: float = 30.0,
        memory_limit_mb: int | None = None,
        statement_timeout_ms: int | None = None,
    ) -> None:
        self.builder_ref = builder_ref
        self.execute_timeout = execute_timeout
        self.memory_limit_mb = memory_limit_mb
        self.statement_timeout_ms = statement_timeout_ms
        self.ns = _NamespaceProxy(self)
        self._child: Any | None = None
        self._conn: Any | None = None
        self._tempdir: str | None = None
        self._lock = threading.RLock()
        self._kill_lock = threading.Lock()

    # ---------------------------------------------------------------- lifecycle
    @staticmethod
    def _child_environment() -> dict[str, str]:
        url = os.environ.get("DB_URL_RO")
        if not url:
            raise RuntimeError("DB_URL_RO is required before starting SubprocessRuntime")
        env = {key: os.environ[key] for key in OS_ENV_KEYS if key in os.environ}
        env["DB_URL_RO"] = url
        pythonpath = os.environ.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + pythonpath if pythonpath else "")
        return env

    def _start_child(self) -> None:
        self._tempdir = tempfile.mkdtemp(prefix="neuro-air-child-")
        context = multiprocessing.get_context("spawn")
        parent_conn, child_conn = context.Pipe(duplex=True)
        process = context.Process(
            target=_child_entry,
            args=(self.builder_ref, child_conn, self.statement_timeout_ms, self._tempdir),
            daemon=True,
        )
        env = self._child_environment()
        # Spawn snapshots os.environ when start() is called. Replacing the
        # mapping briefly under a lock keeps the parent's live environment
        # unchanged for other threads.
        with _ENV_START_LOCK:
            original = os.environ
            os.environ = env  # type: ignore[assignment]
            try:
                process.start()
            finally:
                os.environ = original  # type: ignore[assignment]
        child_conn.close()
        self._child = process
        self._conn = parent_conn
        if not parent_conn.poll(READY_TIMEOUT_SECONDS):
            self._kill_child()
            raise RPCError("child did not become ready")
        ready = parent_conn.recv()
        if not ready.get("ok"):
            self._kill_child()
            raise RPCError(f"child startup failed: {ready.get('error')}")

    def _ensure_child(self) -> None:
        if self._child is None or self._conn is None:
            self._start_child()

    def _kill_child(self) -> None:
        with self._kill_lock:
            process = self._child
            if process is not None and process.is_alive():
                process.kill()
                process.join(timeout=5)
            conn = self._conn
            if conn is not None:
                with contextlib.suppress(OSError):
                    conn.close()
            self._child = None
            self._conn = None

    def _rpc(self, message: dict[str, Any], timeout: float = RPC_TIMEOUT_SECONDS) -> dict[str, Any]:
        with self._lock:
            self._ensure_child()
            assert self._conn is not None
            try:
                self._conn.send(message)
            except (BrokenPipeError, OSError) as error:
                self._kill_child()
                raise RPCError(f"child pipe closed: {error}") from error
            if not self._conn.poll(timeout):
                self._kill_child()
                raise ChildTimeout(f"operation exceeded {timeout:g}s; child killed")
            try:
                return self._conn.recv()
            except (EOFError, OSError) as error:
                self._kill_child()
                raise RPCError(f"child exited: {error}") from error

    # ------------------------------------------------------------- public API
    def execute(self, code: str, first_attempt: bool = True) -> ExecResult:
        started = time.perf_counter()
        try:
            check_code(code)
        except SecurityError as error:
            return ExecResult(
                ok=False,
                stdout="",
                error=f"SecurityError: {error}",
                elapsed_seconds=time.perf_counter() - started,
                first_attempt=first_attempt,
            )

        stop = threading.Event()
        killed = threading.Event()
        watcher: threading.Thread | None = None
        if self.memory_limit_mb:
            try:
                import psutil  # type: ignore[import-not-found]

                def watch_memory() -> None:
                    while not stop.wait(0.2):
                        process = self._child
                        if process is None or not process.is_alive():
                            return
                        try:
                            rss = psutil.Process(process.pid).memory_info().rss
                        except psutil.Error:
                            return
                        if rss > self.memory_limit_mb * 1024 * 1024:
                            self._kill_child()
                            killed.set()
                            return

                watcher = threading.Thread(target=watch_memory, daemon=True)
                watcher.start()
            except ImportError:
                watcher = None

        try:
            response = self._rpc(
                {"op": "execute", "code": code, "first_attempt": first_attempt},
                timeout=self.execute_timeout,
            )
            return response["result"]
        except ChildTimeout as error:
            return ExecResult(
                ok=False,
                stdout="",
                error=f"TimeoutError: {error}; staged state lost",
                elapsed_seconds=time.perf_counter() - started,
                first_attempt=first_attempt,
            )
        except RPCError as error:
            detail = "MemoryLimit: child RSS exceeded cap; staged state lost" if killed.is_set() else str(error)
            return ExecResult(
                ok=False,
                stdout="",
                error=detail,
                elapsed_seconds=time.perf_counter() - started,
                first_attempt=first_attempt,
            )
        finally:
            stop.set()
            if watcher is not None:
                watcher.join(timeout=1)

    def manifest(self) -> dict[str, str]:
        return dict(self._rpc({"op": "manifest"})["result"])

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                with contextlib.suppress(OSError):
                    self._conn.send({"op": "close"})
                    self._conn.poll(2.0)
            self._kill_child()
            if self._tempdir:
                with contextlib.suppress(OSError):
                    shutil.rmtree(self._tempdir, ignore_errors=True)
                self._tempdir = None

    def __enter__(self) -> "SubprocessRuntime":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
