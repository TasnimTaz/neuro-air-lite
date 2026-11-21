"""Day 10 subprocess-runtime tests.

All tests here are DB-less unless explicitly marked. The child is a real spawned
process with a scrubbed environment; no LLM is involved. One test needs the live
read-only database and skips when it is unavailable.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from core.env import load_agent_env
from core.runtime import SecurityError
from core.subprocess_runtime import MAX_STDOUT_CHARS, SubprocessRuntime
from domains.hk.db import make_ro_engine

load_agent_env(scrub=True)


@pytest.fixture
def runtime():
    instance = SubprocessRuntime(execute_timeout=5.0)
    yield instance
    instance.close()


@pytest.fixture(scope="module")
def live_engine():
    try:
        candidate = make_ro_engine()
    except KeyError:
        pytest.skip("DB_URL_RO is not configured")
    try:
        with candidate.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as error:
        candidate.dispose()
        pytest.skip(f"database unavailable: {error}")
    try:
        yield candidate
    finally:
        candidate.dispose()


def test_state_persists_between_executes(runtime):
    assert runtime.execute("x = 41").ok
    result = runtime.execute("print(x + 1)")
    assert result.ok
    assert "42" in result.stdout


def test_manifest_reports_staged_values(runtime):
    runtime.execute("answer = 7\nframe = pd.DataFrame({'a': [1, 2]})")
    manifest = runtime.manifest()
    assert manifest["answer"] == "int"
    assert manifest["frame"].startswith("DataFrame")
    assert "engine" not in manifest


def test_namespace_proxy_supports_get_item_contains_set_and_pop(runtime):
    runtime.execute("staged = 5")
    assert runtime.ns["staged"] == 5
    assert runtime.ns.get("staged") == 5
    assert runtime.ns.get("absent", "fallback") == "fallback"
    assert "staged" in runtime.ns
    assert "absent" not in runtime.ns
    runtime.ns["written"] = 9
    assert runtime.ns["written"] == 9
    assert runtime.ns.pop("written") == 9
    assert "written" not in runtime.ns
    assert runtime.ns.pop("written", None) is None


def test_engine_proxy_exposes_only_the_url(runtime):
    assert "neuro_ro" in str(runtime.ns["engine"].url)


def test_timeout_kills_the_child_and_a_later_execute_works():
    instance = SubprocessRuntime(execute_timeout=0.5)
    try:
        result = instance.execute("while True: pass")
        assert result.ok is False
        assert "TimeoutError" in result.error
        assert "state lost" in result.error

        recovered = instance.execute("print('fresh child')")
        assert recovered.ok is True
        assert "fresh child" in recovered.stdout
    finally:
        instance.close()


def test_child_environment_does_not_inherit_admin_or_llm_keys(monkeypatch, runtime):
    monkeypatch.setenv("DB_URL_ADMIN", "postgresql://admin:secret@db/airdb")
    monkeypatch.setenv("LLM_API_KEY_STRONG", "strong-secret")
    keys = runtime._rpc({"op": "env_keys"})["result"]
    assert "DB_URL_RO" in keys
    assert "DB_URL_ADMIN" not in keys
    assert "POSTGRES_PASSWORD" not in keys
    assert "LLM_API_KEY_STRONG" not in keys


def test_child_cwd_is_a_fresh_empty_temp_dir(runtime):
    probe = runtime._rpc({"op": "cwd_probe"})["result"]
    assert probe["env_exists"] is False
    assert os.path.basename(probe["cwd"]).startswith("neuro-air-child-")


def test_ast_checker_blocks_code_before_it_is_sent(monkeypatch, runtime):
    def reject(code):
        raise SecurityError("blocked by test")

    monkeypatch.setattr("core.subprocess_runtime.check_code", reject)
    sent: list[dict] = []

    def fake_rpc(message, timeout=60.0):
        sent.append(message)
        raise AssertionError("RPC must not be called")

    monkeypatch.setattr(runtime, "_rpc", fake_rpc)
    result = runtime.execute("print('never sent')")
    assert result.ok is False
    assert "SecurityError" in result.error
    assert sent == []


def test_two_concurrent_runtimes_do_not_mix_stdout():
    first = SubprocessRuntime(execute_timeout=10.0)
    second = SubprocessRuntime(execute_timeout=10.0)
    # Spawn both children before the timed section; Windows spawn from a worker
    # thread is unnecessary for this assertion and would make the test flaky.
    first._ensure_child()
    second._ensure_child()
    barrier = threading.Barrier(2)

    def run(instance, token):
        barrier.wait()
        return instance.execute(f"print('{token}')")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_one = pool.submit(run, first, "AAA")
            future_two = pool.submit(run, second, "BBB")
            out_one = future_one.result(timeout=20)
            out_two = future_two.result(timeout=20)
        assert "AAA" in out_one.stdout and "BBB" not in out_one.stdout
        assert "BBB" in out_two.stdout and "AAA" not in out_two.stdout
    finally:
        first.close()
        second.close()


def test_large_stdout_is_truncated(runtime):
    result = runtime.execute("print('x' * 50000)")
    assert result.ok is True
    assert "[truncated]" in result.stdout
    assert len(result.stdout) <= MAX_STDOUT_CHARS + len("\n... [truncated]")


def test_child_can_read_database_and_cannot_insert(live_engine, runtime):
    read = runtime.execute(
        "with engine.connect() as conn:\n"
        "    print(conn.execute(text('SELECT count(*) FROM air_quality_station')).scalar_one())"
    )
    assert read.ok is True
    assert read.stdout.strip().isdigit()

    # Bypass the parent AST checker to prove the DB role, not the checker, is
    # the data boundary. The child deliberately does not call check_code().
    response = runtime._rpc(
        {
            "op": "execute",
            "code": "with engine.connect() as conn:\n"
            "    conn.execute(text('SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE'))\n"
            "    conn.commit()\n"
            "    conn.execute(text('INSERT INTO air_quality_station SELECT * FROM air_quality_station LIMIT 1'))\n",
            "first_attempt": True,
        }
    )
    denied = response["result"]
    assert denied.ok is False
    assert "42501" in denied.error or "permission denied" in denied.error.lower()
