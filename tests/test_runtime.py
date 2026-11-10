"""Tests for PythonRuntime, the AST security checker, and agent isolation."""

import os

import pandas as pd
import pytest
from sqlalchemy.exc import OperationalError

from core.env import admin_url, load_agent_env
from core.runtime import ExecResult, PythonRuntime, check_code
from domains.hk.tools import build_runtime


def test_code_execution_persistence():
    rt = PythonRuntime()
    res1 = rt.execute("x = 42\nprint('stored')")
    assert res1.ok
    assert "stored" in res1.stdout

    res2 = rt.execute("y = x * 2\nprint(y)")
    assert res2.ok
    assert "84" in res2.stdout
    assert rt.ns["x"] == 42
    assert rt.ns["y"] == 84


def test_manifest_filters_injected_and_callables():
    rt = PythonRuntime(variables={"injected_var": 123, "pd": pd})
    rt.execute("calc_var = 999\nmy_str = 'hello'")
    manifest = rt.manifest()

    assert "injected_var" not in manifest
    assert "pd" not in manifest
    assert "__name__" not in manifest
    assert manifest["calc_var"] == "int"
    assert manifest["my_str"] == "str"


@pytest.mark.parametrize(
    "bad_code",
    [
        "import os",
        "import sys",
        "from subprocess import Popen",
        "import shutil",
        "import urllib.request",
        "import requests",
        "import httpx",
        "import aiohttp",
        "from httpx import get",
        "open('some_file.txt', 'w')",
        "eval('1 + 1')",
        "exec('print(1)')",
        "compile('x = 1', '', 'exec')",
        "getattr(int, '__class__')",
        "setattr(rt, 'x', 1)",
        "globals()",
        "locals()",
        "vars()",
        "x = (1).__class__",
        "x = (1).__doc__",
        # The reported bypass: pandas already holds a reference to `os`.
        "print(pd.io.common.os.environ.get('DB_URL_ADMIN'))",
        "print(pd.io.common.os.getenv('DB_URL_ADMIN'))",
        "pd.io.common.os.system('whoami')",
        "print(pd.io.common.os.environ)",
        "__builtins__['eval']('1 + 1')",
        "print(__builtins__)",
        "s = 'DROP TABLE students'",
        "s = 'DELETE FROM air_quality_station'",
        "s = 'SET statement_timeout = 0'",
        "s = 'SELECT 1; SET statement_timeout = 0'",
        "s = 'SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE'",
        "s = 'TRUNCATE TABLE hourly_station_weather'",
        "s = '/* note */ ALTER TABLE foo DROP COLUMN bar'",
        "tbl = 'station'\ns = f'INSERT INTO {tbl} VALUES (1)'",
    ],
)
def test_security_rules_block_forbidden_patterns(bad_code: str):
    rt = PythonRuntime(variables={"pd": pd})
    res = rt.execute(bad_code)
    assert not res.ok
    assert "SecurityError" in res.error


@pytest.mark.parametrize(
    "good_code",
    [
        # Labels are not SQL. The old `^\\s*(insert|update|...)` guard rejected all
        # of these and cost the model a step for a harmless print.
        "print('Set of stations:', 3)",
        "print('Update: mean PM2.5')",
        "print('Do the stations differ?')",
        "print('Call it Tai Po')",
        "print('Insert the values into the report')",
        "print('RESET the counter')",
        "print('copy to clipboard')",
        "print('Delete the outlier rows in pandas')",
        # Real read-only SQL must pass.
        "sql = \"SELECT count(*) FROM air_quality_station WHERE station_type = 'ROADSIDE'\"",
        "sql = 'SELECT 1'",
        "sql = 'SELECT station_type, count(*) FROM air_quality_station GROUP BY 1'",
        "sql = \"SELECT max(datetime) FROM hourly_station_air_quality\"",
    ],
)
def test_sql_guard_allows_read_only_labels(good_code: str):
    rt = PythonRuntime()
    res = rt.execute(good_code)
    assert res.ok, res.error


def test_syntax_and_runtime_errors_return_exec_result():
    rt = PythonRuntime()

    # Syntax Error
    res_syntax = rt.execute("def unclosed_func(")
    assert not res_syntax.ok
    assert "SyntaxError" in res_syntax.error

    # Runtime ZeroDivisionError
    res_runtime = rt.execute("x = 1 / 0")
    assert not res_runtime.ok
    assert "ZeroDivisionError" in res_runtime.error


def test_render_limit_and_truncation():
    res = ExecResult(ok=True, stdout="A" * 15000, error="")
    rendered = res.render(limit=1000)
    assert len(rendered) <= 1050
    assert "... [truncated]" in rendered


def test_agent_env_has_no_admin_dsn(monkeypatch):
    """The agent loader must not export the admin DSN, and still must not leak it.

    This test must pass on a clean checkout with no `.env` and no admin file, so
    it patches the two file readers instead of depending on local secrets.
    """
    import core.env as env_module

    ro_url = "postgresql+psycopg2://neuro_ro:test@127.0.0.1:5432/airdb"
    admin_dsn = "postgresql+psycopg2://postgres:test@127.0.0.1:5432/airdb"

    monkeypatch.setattr(env_module, "_file_values", lambda: {"DB_URL_RO": ro_url})
    monkeypatch.setattr(env_module, "_admin_values", lambda: {"DB_URL_ADMIN": admin_dsn})
    monkeypatch.setenv("DB_URL_RO", ro_url)
    for key in (
        "DB_URL_ADMIN",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
        "POSTGRES_DB",
        "NEURO_ADMIN_ENV",
    ):
        monkeypatch.delenv(key, raising=False)

    load_agent_env(override=True, scrub=True)

    assert os.environ.get("DB_URL_RO") == ro_url
    assert "DB_URL_ADMIN" not in os.environ
    assert "POSTGRES_PASSWORD" not in os.environ

    # Truth queries still get the DSN, returned rather than exported.
    assert admin_url() == admin_dsn
    assert "DB_URL_ADMIN" not in os.environ


def test_runtime_only_gets_ro_engine():
    """build_runtime injects the read-only engine and nothing admin-shaped."""
    rt = build_runtime()
    try:
        assert "engine" in rt.ns
        assert "DB_URL_ADMIN" not in rt.ns
        assert "neuro_ro" in str(rt.ns["engine"].url)
    finally:
        rt.close()


def test_ro_role_cannot_insert():
    """The GRANT layer, exercised through the exact engine the agent receives.

    A plain INSERT fails at 25006 (read-only transaction default) before
    privileges are consulted, which proves the session default but not the
    grants. The test lifts the default the same way check_db.py does, then
    requires 42501 from the missing INSERT privilege.
    """
    rt = build_runtime()
    try:
        try:
            with rt.ns["engine"].connect() as conn:
                conn.exec_driver_sql("SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE")
                conn.commit()
                try:
                    conn.exec_driver_sql(
                        "INSERT INTO air_quality_station (station_code) VALUES ('ro_write_probe')"
                    )
                except Exception as error:  # noqa: BLE001
                    code = getattr(getattr(error, "orig", None), "pgcode", None)
                    assert code == "42501", f"expected SQLSTATE 42501, got {code}: {error}"
                else:
                    pytest.fail("neuro_ro accepted an INSERT once read-only was lifted")
                finally:
                    conn.rollback()
        except OperationalError as error:
            pytest.skip(f"database not reachable: {error.orig}")
    finally:
        rt.close()


def test_check_code_accepts_plain_dataframe_code():
    check_code(
        "df = pd.read_sql(sql, engine)\n"
        "print(df.describe())\n"
        "ANSWER = float(df['pm2_5'].mean())\n"
    )


def test_check_code_accepts_injected_tools():
    check_code(
        "location = geocode('Tai Po, Hong Kong')\n"
        "charts.line('t', ['a', 'b'], {'s': [1.0, 2.0]})\n"
        "print(location['latitude'], len(charts.charts))\n"
    )


def test_runtime_injects_host_tools_and_hides_them_from_manifest():
    rt = build_runtime()
    try:
        assert callable(rt.ns["geocode"])
        assert hasattr(rt.ns["charts"], "line")

        result = rt.execute(
            "charts.line('t', ['a', 'b'], {'s': [1.0, float('nan')]})\n"
            "print(len(charts.charts), charts.charts[0]['option']['series'][0]['data'])"
        )
        assert result.ok, result.error
        assert "1 [1.0, None]" in result.stdout

        manifest = rt.manifest()
        assert "geocode" not in manifest
        assert "charts" not in manifest
    finally:
        rt.close()
