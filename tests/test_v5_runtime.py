"""v5 runtime: clean exit() handling and numpy binding adapters."""

import numpy as np
import pytest

from core.runtime import SecurityError
from core.runtime_v5 import HALT_NOTE, PythonRuntimeV5, register_numpy_adapters, rewrite_exit_calls


def test_exit_statement_halts_cell_and_keeps_state():
    rt = PythonRuntimeV5()
    res = rt.execute("x = 1\nprint('before')\nexit()\nx = 2\nprint('after')")
    assert res.ok and "before" in res.stdout and "after" not in res.stdout
    assert HALT_NOTE in res.stdout
    assert rt.ns["x"] == 1


@pytest.mark.parametrize("stmt", ["exit()", "quit()", "exit(0)", "quit(1)"])
def test_exit_variants_are_rewritten(stmt):
    rt = PythonRuntimeV5()
    assert rt.execute(f"y = 5\n{stmt}\ny = 6").ok
    assert rt.ns["y"] == 5


def test_exit_inside_if_and_except_exception_cannot_swallow_it():
    rt = PythonRuntimeV5()
    res = rt.execute("a = 1\ntry:\n    if a:\n        exit()\nexcept Exception:\n    a = 99\na = 3")
    assert res.ok
    assert rt.ns["a"] == 1  # BaseException passes through `except Exception`


def test_exit_used_as_a_value_is_still_blocked():
    rt = PythonRuntimeV5()
    res = rt.execute("f = exit")
    assert not res.ok and "forbidden name: exit" in res.error


def test_other_security_rules_unchanged():
    rt = PythonRuntimeV5()
    assert "forbidden import" in rt.execute("import os").error
    assert "forbidden call" in rt.execute("eval('1')").error
    assert "write/DDL" in rt.execute("q = 'DROP TABLE x'").error


def test_errors_and_manifest_behave_like_v4():
    rt = PythonRuntimeV5({"k": 1})
    assert not rt.execute("1/0").ok
    rt.execute("val = [1, 2]")
    assert "val" in rt.manifest() and "_HaltCell" not in rt.manifest() and "k" not in rt.manifest()


def test_rewrite_only_touches_exit_call_statements():
    out = rewrite_exit_calls("print(1)\nexit()\nz = 2")
    assert "_HaltCell" in out and "exit()" not in out and "z = 2" in out
    with pytest.raises(SyntaxError):
        rewrite_exit_calls("def (")


def test_numpy_scalars_are_adaptable_by_psycopg2():
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2.extensions import adapt

    assert register_numpy_adapters()
    assert adapt(np.int64(7)).getquoted() == b"7"
    assert adapt(np.int32(-3)).getquoted() == b"-3"
    assert adapt(np.float64(1.5)).getquoted() in (b"1.5",)
    assert adapt(np.bool_(True)).getquoted() == b"true"
    assert adapt(np.float64("nan")).getquoted() == b"'NaN'::float"
