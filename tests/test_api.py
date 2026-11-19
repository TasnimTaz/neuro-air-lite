"""Fake-LLM tests for the Day 7 API and SSE stream.

Nothing here touches Ollama or Postgres: ``create_app`` accepts a runtime factory
and an LLM callable, so the tests prove the request wiring, the single-flight
gate, the timeout path, per-request runtime cleanup and the static UI without a
live model.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from api.app import BusyGate, build_reference_payload, create_app
from core.runtime import PythonRuntime
from domains.hk.viz import EChartsConfigManager


class ScriptedLLM:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0

    async def __call__(self, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        if not self.responses:
            raise AssertionError("scripted LLM ran out of responses")
        return self.responses.pop(0)


class RecordingLLM(ScriptedLLM):
    def __init__(self, responses: list[str]):
        super().__init__(responses)
        self.messages: list[list[dict[str, str]]] = []

    async def __call__(self, messages: list[dict[str, str]]) -> str:
        self.messages.append([dict(message) for message in messages])
        return await super().__call__(messages)


class TrackingRuntime(PythonRuntime):
    def __init__(self) -> None:
        super().__init__(variables={"charts": EChartsConfigManager()})
        self.closed = False

    def close(self) -> None:
        self.closed = True
        super().close()


class BlockingRuntime(PythonRuntime):
    """A runtime whose execute call outlives the HTTP timeout."""

    def __init__(self, release: threading.Event) -> None:
        super().__init__(variables={"charts": EChartsConfigManager()})
        self.release = release

    def execute(self, code: str, first_attempt: bool = True):
        self.release.wait(timeout=5.0)
        return super().execute(code, first_attempt=first_attempt)


def _factory() -> PythonRuntime:
    return PythonRuntime(variables={"charts": EChartsConfigManager()})


def _client(
    llm: ScriptedLLM | object,
    *,
    runtime_factory=_factory,
    timeout_seconds: float = 5.0,
    heartbeat_seconds: float = 0.01,
) -> tuple[object, TestClient]:
    app = create_app(
        runtime_factory=runtime_factory,
        llm_func=llm,  # type: ignore[arg-type]
        timeout_seconds=timeout_seconds,
        heartbeat_seconds=heartbeat_seconds,
    )
    return app, TestClient(app)


def _events(body: str) -> list[tuple[str, object]]:
    events: list[tuple[str, object]] = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        name = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())
        if name and data_lines:
            events.append((name, json.loads("\n".join(data_lines))))
    return events


SINGLE_RESPONSES = [
    "```python\nANSWER = 42\nprint(ANSWER)\n"
    "charts.bar('Demo', ['A', 'B'], {'PM2.5': [42, 7]})\n```",
    "The value is 42 micrograms per cubic metre.",
]

MULTI_RESPONSES = [
    '[{"role": "data_acquisition", "task": "stage 41"}, '
    '{"role": "synthesis_report", "task": "answer"}]',
    "```python\nstaged = 41\nprint(staged)\n```",
    "staged value ready",
    "```python\nANSWER = staged + 1\nprint(ANSWER)\n```",
    "The answer is 42.",
    "```python\nprint('verified')\nANSWER = 42\n```",
    "Verified: 42.",
]

MULTI_CHANGED_RESPONSES = [
    '[{"role": "data_acquisition", "task": "stage 41"}, '
    '{"role": "synthesis_report", "task": "answer"}]',
    "```python\nstaged = 41\nprint(staged)\n```",
    "staged value ready",
    "```python\nANSWER = staged + 1\nprint(ANSWER)\n```",
    "The answer is 42.",
    "```python\nprint('correcting')\nANSWER = 43\nprint(ANSWER)\n```",
    "Verifier final: 43.",
]


def test_single_arm_is_default_and_streams_final_with_charts():
    app, client = _client(ScriptedLLM(SINGLE_RESPONSES))
    response = client.post("/api/chat", json={"query": "demo question"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    names = [name for name, _ in events]
    assert names[0] == "start"
    assert "final" in names
    assert names[-1] == "done"
    assert app.state.gate.busy is False

    start = next(data for name, data in events if name == "start")
    final = next(data for name, data in events if name == "final")
    done = next(data for name, data in events if name == "done")

    assert start["arm"] == "single"
    assert final["arm"] == "single"
    assert final["answer"] == 42
    assert final["final_text"] == "The value is 42 micrograms per cubic metre."
    assert final["charts"][0]["title"] == "Demo"
    assert final["charts"][0]["option"]["series"][0]["type"] == "bar"
    assert done["ok"] is True


def test_single_arm_uses_the_same_answer_hint_as_the_evaluated_run():
    llm = RecordingLLM(SINGLE_RESPONSES)
    _, client = _client(llm)
    response = client.post("/api/chat", json={"query": "demo question"})

    assert response.status_code == 200
    assert llm.messages
    assert any(
        "assign it to a variable named ANSWER" in message["content"]
        for message in llm.messages[0]
    )


def test_multi_arm_emits_a_plan_and_runs_the_verifier():
    _, client = _client(ScriptedLLM(MULTI_RESPONSES))
    response = client.post("/api/chat", json={"query": "demo question", "arm": "multi"})

    assert response.status_code == 200
    events = _events(response.text)
    plan = next(data for name, data in events if name == "plan")
    final = next(data for name, data in events if name == "final")

    assert [step["role"] for step in plan] == ["data_acquisition", "synthesis_report"]
    assert final["arm"] == "multi"
    assert final["answer"] == 42
    assert final["plan_source"] == "llm"
    assert final["verifier_mode"] == "overwrite"
    assert final["verifier_status"] == "confirmed"
    assert final["verifier_changed_answer"] is False
    assert final["answer_pre_verify"] == 42
    assert final["synthesis_draft"] == ""
    assert final["final_text"] == "The answer is 42."
    assert final["verifier_audit_text"] == "Verified: 42."


def test_multi_arm_keeps_final_text_consistent_when_verifier_changes_answer():
    _, client = _client(ScriptedLLM(MULTI_CHANGED_RESPONSES))
    response = client.post("/api/chat", json={"query": "demo question", "arm": "multi"})

    assert response.status_code == 200
    final = next(data for name, data in _events(response.text) if name == "final")

    assert final["answer"] == 43
    assert final["answer_pre_verify"] == 42
    assert final["verifier_status"] == "changed"
    assert final["verifier_changed_answer"] is True
    assert final["final_text"] == "Verifier final: 43."
    assert final["synthesis_draft"] == "The answer is 42."
    assert final["verifier_audit_text"] == "Verifier final: 43."


def test_preview_banner_is_served_only_when_enabled():
    app = create_app(
        runtime_factory=_factory,
        llm_func=ScriptedLLM([]),
        preview_banner=True,
    )
    client = TestClient(app)
    assert "SYNTHETIC DEMO DATA" in client.get("/").text


def test_query_alias_and_validation():
    _, client = _client(ScriptedLLM(SINGLE_RESPONSES))
    response = client.post("/api/chat", json={"message": " demo question "})
    assert response.status_code == 200
    final = next(data for name, data in _events(response.text) if name == "final")
    assert final["query"] == "demo question"

    blank = client.post("/api/chat", json={"query": "   "})
    assert blank.status_code == 422

    bad_arm = client.post("/api/chat", json={"query": "x", "arm": "both"})
    assert bad_arm.status_code == 422


def test_busy_gate_returns_429_immediately():
    app, client = _client(ScriptedLLM(SINGLE_RESPONSES))
    lease = app.state.gate.try_acquire()
    assert lease is not None
    try:
        response = client.post("/api/chat", json={"query": "demo"})
    finally:
        lease.release()

    assert response.status_code == 429
    assert response.headers["retry-after"] == "2"
    assert "already running" in response.json()["detail"]
    assert app.state.gate.busy is False


def test_busy_gate_is_idempotent():
    gate = BusyGate()
    first = gate.try_acquire()
    assert first is not None
    assert gate.try_acquire() is None
    first.release()
    first.release()
    assert gate.busy is False


def test_timeout_emits_an_error_instead_of_hanging():
    async def slow_llm(messages):
        await asyncio.sleep(0.2)
        return "too late"

    _, client = _client(slow_llm, timeout_seconds=0.02, heartbeat_seconds=0.005)
    response = client.post("/api/chat", json={"query": "slow"})

    assert response.status_code == 200
    events = _events(response.text)
    error = next(data for name, data in events if name == "error")
    done = next(data for name, data in events if name == "done")
    assert error["type"] == "TimeoutError"
    assert "Timed out" in error["message"]
    assert done["ok"] is False


def test_timeout_poisons_app_while_runtime_thread_is_still_executing():
    release = threading.Event()
    app, client = _client(
        ScriptedLLM(SINGLE_RESPONSES),
        runtime_factory=lambda: BlockingRuntime(release),
        timeout_seconds=0.05,
        heartbeat_seconds=0.01,
    )
    try:
        response = client.post("/api/chat", json={"query": "blocked"})
        assert response.status_code == 200
        events = _events(response.text)
        error = next(data for name, data in events if name == "error")
        assert error["type"] == "TimeoutError"
        assert "runtime still executing" in error["message"]

        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["degraded"] is True
        assert health.json()["status"] == "degraded"

        blocked = client.post("/api/chat", json={"query": "again"})
        assert blocked.status_code == 503
        assert blocked.json() == {
            "error": "runtime still executing; restart the server"
        }

        # Releasing the worker thread does not clear the poison; only a process
        # restart does, because the runtime was used after its request ended.
        release.set()
        time.sleep(0.1)
        assert client.get("/api/health").json()["degraded"] is True
        assert client.post("/api/chat", json={"query": "again"}).status_code == 503
    finally:
        release.set()


def test_each_request_gets_and_closes_its_own_runtime():
    created: list[TrackingRuntime] = []

    def factory() -> TrackingRuntime:
        runtime = TrackingRuntime()
        created.append(runtime)
        return runtime

    _, client = _client(ScriptedLLM(SINGLE_RESPONSES * 2), runtime_factory=factory)
    assert client.post("/api/chat", json={"query": "one"}).status_code == 200
    assert client.post("/api/chat", json={"query": "two"}).status_code == 200

    assert len(created) == 2
    assert created[0] is not created[1]
    assert all(runtime.closed for runtime in created)


def test_create_app_scrubs_admin_dsn(monkeypatch):
    monkeypatch.setenv(
        "DB_URL_ADMIN",
        "postgresql+psycopg2://admin:secret@127.0.0.1:5432/airdb",
    )
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")

    create_app(runtime_factory=_factory, llm_func=ScriptedLLM([]))

    assert "DB_URL_ADMIN" not in os.environ
    assert "POSTGRES_PASSWORD" not in os.environ


def test_static_ui_and_health_are_served():
    app, client = _client(ScriptedLLM([]))
    index = client.get("/")
    assert index.status_code == 200
    assert "Neuro-Air Lite" in index.text
    assert "echarts" in index.text
    assert client.get("/static/app.js").status_code == 200

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["degraded"] is False
    assert health.json()["arm_default"] == "single"
    assert health.json()["busy"] is False


def test_reference_endpoint_serves_snapshot_and_chart_configs_without_a_model():
    _, client = _client(ScriptedLLM([]))
    response = client.get("/api/reference/tai_po")

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "tai_po_fire_reference"
    assert payload["ok"] is True
    assert payload["observed"]["no2_at_2000"] == 123.2
    assert payload["anomaly_assessment"]["matched_hour_z"] == pytest.approx(1.9106, abs=0.001)
    assert payload["anomaly_assessment"]["supports_local_plume"] == "false"
    assert len(payload["charts"]) == 2
    assert payload["charts"][0]["option"]["series"][0]["type"] == "bar"
    assert payload["charts"][1]["title"] == "PM2.5 evening rise: Tai Po vs other 17"


def test_reference_payload_is_file_only():
    payload = build_reference_payload()
    assert payload["ok"] is True
    assert payload["charts"]
    assert "no LLM" not in json.dumps(payload["charts"])  # configs only, no prompt text
