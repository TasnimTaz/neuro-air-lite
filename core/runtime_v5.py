"""v5 in-process runtime: removes harness friction that is not part of the architecture.

The v2-v4 failure logs show a large share of failed case-runs were caused by the
*harness*, not by the model or the multi-agent topology:

* ``exit()`` / ``quit()`` are blocked by the AST checker, so a worker that tries
  to stop cleanly gets a SecurityError, retries the same block, trips the stall
  guard and ends on the stop sentinel (8 of 19 v2 failures, 8 of 20 v3 failures).
* ``numpy.int64`` values (what pandas hands back for an id) cannot be bound by
  psycopg2 ("can't adapt type 'numpy.int64'"), so a correct station id fails the
  next query (v3 #14, #15, #17).

The paper reports 99.3% first-attempt execution success. Until first-attempt
success is comparably high in *both* arms, an architecture comparison mostly
measures harness friction. Those errors hit the multi-agent arm hardest because
a failed handoff cannot be repaired by the next worker.

Nothing here changes the security model: ``check_code`` still runs on the
(rewritten) source, and the read-only DB role remains the real boundary.

``exit()``/``quit()`` handling
    A *call statement* ``exit()``/``quit()`` (any arguments) is rewritten to
    ``raise _HaltCell()``. The cell stops where the model asked it to stop,
    already-staged variables are kept, and the result is ``ok=True`` with a
    one-line note in stdout. Any other use of ``exit``/``quit`` (e.g. passing it
    around as a value) is left alone and still rejected by ``check_code``.
"""

from __future__ import annotations

import ast
import contextlib
import io
import time
import traceback
from typing import Any

from core.runtime import ExecResult, PythonRuntime, SecurityError, check_code

HALT_NOTE = "[exit() ignored: this cell stopped here; variables staged so far are kept]"


class _HaltCell(BaseException):
    """Raised by a rewritten ``exit()``; BaseException so ``except Exception`` can't swallow it."""


class _ExitRewriter(ast.NodeTransformer):
    """Rewrite statement-level ``exit(...)`` / ``quit(...)`` into ``raise _HaltCell()``."""

    def visit_Expr(self, node: ast.Expr) -> ast.AST:  # noqa: N802 - ast API
        call = node.value
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id in {"exit", "quit"}
        ):
            raise_stmt = ast.Raise(
                exc=ast.Call(func=ast.Name(id="_HaltCell", ctx=ast.Load()), args=[], keywords=[]),
                cause=None,
            )
            return ast.copy_location(raise_stmt, node)
        return self.generic_visit(node)


def rewrite_exit_calls(src: str) -> str:
    """Return ``src`` with statement-level exit()/quit() replaced; syntax errors pass through."""
    tree = ast.parse(src)
    new_tree = _ExitRewriter().visit(tree)
    ast.fix_missing_locations(new_tree)
    return ast.unparse(new_tree)


def register_numpy_adapters() -> bool:
    """Let psycopg2 bind numpy scalars. Returns False when psycopg2/numpy are missing.

    Idempotent. Must also be called inside any child process that opens its own
    DB connection (see ``core/subprocess_runtime.py``) because adapters are
    process-global.
    """
    try:
        import numpy as np
        from psycopg2.extensions import AsIs, register_adapter
    except ImportError:  # pragma: no cover - psycopg2 is a hard dependency in prod
        return False

    for t in (np.int8, np.int16, np.int32, np.int64, np.uint8, np.uint16, np.uint32, np.uint64):
        register_adapter(t, lambda v: AsIs(int(v)))
    for t in (np.float16, np.float32, np.float64):
        register_adapter(t, lambda v: AsIs("'NaN'::float" if v != v else repr(float(v))))
    register_adapter(np.bool_, lambda v: AsIs("true" if bool(v) else "false"))
    return True


register_numpy_adapters()


class PythonRuntimeV5(PythonRuntime):
    """``PythonRuntime`` with clean ``exit()`` handling. Same namespace/manifest/close semantics."""

    def __init__(self, variables: dict[str, Any] | None = None):
        super().__init__(variables)
        self.ns["_HaltCell"] = _HaltCell
        self._injected.add("_HaltCell")

    def execute(self, code: str, first_attempt: bool = True) -> ExecResult:
        started = time.perf_counter()
        buf = io.StringIO()

        def result(ok: bool, error: str = "") -> ExecResult:
            return ExecResult(
                ok=ok,
                stdout=buf.getvalue(),
                error=error,
                elapsed_seconds=time.perf_counter() - started,
                first_attempt=first_attempt,
            )

        try:
            rewritten = rewrite_exit_calls(code)
            check_code(rewritten)
            try:
                with contextlib.redirect_stdout(buf):
                    exec(compile(rewritten, "<agent>", "exec"), self.ns)
            except _HaltCell:
                buf.write(("\n" if buf.getvalue() and not buf.getvalue().endswith("\n") else "") + HALT_NOTE + "\n")
            return result(True)
        except SecurityError as e:
            return result(False, f"SecurityError: {e}")
        except Exception:
            return result(False, traceback.format_exc(limit=3))
