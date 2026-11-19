"""FastAPI app for the Neuro-Air Lite demo.

The chat endpoint is deliberately single-flight. ``PythonRuntime.execute`` uses
``contextlib.redirect_stdout``, which is process-wide, so two agent runs in one
process would interleave each other's stdout. A second request therefore gets
``429`` immediately instead of waiting in a queue or corrupting the first trace.

Every request gets its own ``PythonRuntime`` and its own read-only engine. The
admin DSN is never read here: ``build_runtime()`` calls ``load_agent_env()``,
which scrubs admin-only keys before the model code can reach ``os.environ``.

The browser UI consumes Server-Sent Events from ``POST /api/chat``. The agent is
not a token-streaming model: it emits a plan/worker/code/output trace and one
final event with the answer and any ECharts configs built in the runtime.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from starlette.background import BackgroundTask

from core.agent import run_worker
from core.env import load_agent_env
from core.llm import llm, snapshot_usage
from core.orchestrator import run_pipeline
from core.runtime import PythonRuntime
from domains.hk.roles import COORDINATOR_SYSTEM, MULTI_RULES, ROLE_PROMPTS
from domains.hk.schema_prompt import get_system_prompt
from domains.hk.tools import build_runtime
from domains.hk.viz import EChartsConfigManager
from evals.hk_baseline_v2 import ANSWER_HINT

STATIC_DIR = Path(__file__).resolve().parent / "static"
REFERENCE_PATH = Path(__file__).resolve().parents[1] / "docs" / "tai_po_reference.json"

REQUEST_TIMEOUT_SECONDS = 600.0
HEARTBEAT_SECONDS = 15.0
MAX_QUERY_CHARS = 4000
MAX_ERROR_CHARS = 500

RuntimeFactory = Callable[[], PythonRuntime]
LLMFunc = Callable[[list[dict[str, str]]], Awaitable[str]]


class ChatRequest(BaseModel):
    """One UI question.

    ``query`` is the documented field; the aliases keep small clients that call
    it ``question`` or ``message`` working without a second endpoint.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    query: str = Field(
        ...,
        min_length=1,
        max_length=MAX_QUERY_CHARS,
        validation_alias=AliasChoices("query", "question", "message"),
    )
    arm: Literal["single", "multi"] = "single"

    @field_validator("query")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value


@dataclass
class _Lease:
    """Idempotent ownership token for the single-flight gate."""

    gate: "BusyGate"
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self.gate._busy = False


class BusyGate:
    """A synchronous try-acquire gate used by an async route.

    The route runs on the event-loop thread, so checking and setting ``_busy``
    without an ``await`` is atomic with respect to other requests.
    """

    def __init__(self) -> None:
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def try_acquire(self) -> _Lease | None:
        if self._busy:
            return None
        self._busy = True
        return _Lease(self)


class RuntimeGuard:
    """Detect a runtime that outlived its request.

    ``asyncio.wait_for`` cancels the asyncio task, but a worker thread blocked
    in ``PythonRuntime.execute`` keeps running. Because that method redirects
    process-wide stdout, starting a second run would corrupt both traces. If a
    timeout or client disconnect leaves execution in flight, the app is marked
    poisoned and refuses new chat requests until the process is restarted.
    """

    def __init__(self) -> None:
        self.poisoned = False
        self.runtime: PythonRuntime | None = None

    def attach(self, runtime: PythonRuntime) -> None:
        self.runtime = runtime
        original_execute = runtime.execute
        runtime.running = False  # type: ignore[attr-defined]

        def tracked_execute(*args: Any, **kwargs: Any) -> Any:
            runtime.running = True  # type: ignore[attr-defined]
            try:
                return original_execute(*args, **kwargs)
            finally:
                runtime.running = False  # type: ignore[attr-defined]

        runtime.execute = tracked_execute  # type: ignore[method-assign]

    def observe(self) -> bool:
        """Mark the app poisoned if the attached runtime is still executing."""
        if self.runtime is not None and getattr(self.runtime, "running", False):
            self.poisoned = True
        return self.poisoned

    def close_if_idle(self, runtime: PythonRuntime) -> None:
        if getattr(runtime, "running", False):
            self.poisoned = True
            return
        runtime.close()
        if self.runtime is runtime:
            self.runtime = None


def _jsonable(value: Any) -> Any:
    """Make runtime values JSON-safe without turning every number into a string."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]

    # numpy scalars expose .item(); pandas objects often expose .tolist().
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _jsonable(item())
        except (TypeError, ValueError):
            pass
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return _jsonable(tolist())
        except (TypeError, ValueError):
            pass
    return str(value)


def _sse(event: str, payload: Any) -> str:
    data = json.dumps(_jsonable(payload), ensure_ascii=False, allow_nan=False)
    return f"event: {event}\ndata: {data}\n\n"


def _safe_error(exc: BaseException) -> str:
    """Return a short error string with any URL password redacted."""
    message = f"{type(exc).__name__}: {exc}".strip()
    message = re.sub(r"(?<=://)[^@\s/]+@", "<redacted>@", message)
    return message[:MAX_ERROR_CHARS]


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _usage_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    return {
        "prompt_tokens": int(after["prompt"] - before["prompt"]),
        "completion_tokens": int(after["completion"] - before["completion"]),
        "calls": int(after["calls"] - before["calls"]),
    }


async def _run_chat(
    *,
    query: str,
    arm: Literal["single", "multi"],
    runtime_factory: RuntimeFactory,
    llm_func: LLMFunc,
    verifier_mode: str,
    guard: RuntimeGuard,
    emit: Callable[[str, Any], None],
) -> dict[str, Any]:
    """Run one request on a fresh runtime and return the final event payload."""
    runtime = runtime_factory()
    guard.attach(runtime)
    started = time.perf_counter()
    usage_before = snapshot_usage()
    try:
        system = get_system_prompt(engine=runtime.ns.get("engine"))
        if arm == "multi":
            pipeline = await run_pipeline(
                llm=llm_func,
                runtime=runtime,
                role_prompts=ROLE_PROMPTS,
                shared_rules=system + "\n\n" + MULTI_RULES,
                coordinator_system=COORDINATOR_SYSTEM,
                query=query,
                task_suffix=ANSWER_HINT,
                max_total=_int_env("MAX_TOTAL_STEPS", 20),
                verify_reserve=_int_env("VERIFY_RESERVE", 6),
                synth_reserve=_int_env("SYNTH_RESERVE", 3),
                verifier_mode=verifier_mode,
                emit=emit,
            )
            synthesis_narrative = (pipeline.final_text_pre_verify or "").strip()
            verifier_text = (pipeline.verifier_text or "").strip()

            # The public final_text must agree with the public ANSWER. Keep the
            # richer synthesis narrative only when the verifier left ANSWER
            # unchanged; otherwise send the verifier's final text and expose the
            # synthesis draft separately for the UI.
            synthesis_draft = ""
            if pipeline.verifier_changed_answer:
                final_text = pipeline.final_text
                if synthesis_narrative and synthesis_narrative != final_text:
                    synthesis_draft = synthesis_narrative
            else:
                final_text = synthesis_narrative or pipeline.final_text

            if pipeline.verifier_mode == "none":
                verifier_status = "not_run"
            elif pipeline.verifier_changed_answer:
                verifier_status = "changed"
            elif pipeline.verifier_mismatch:
                verifier_status = "mismatch"
            elif not verifier_text:
                verifier_status = "failed"
            else:
                verifier_status = "confirmed"

            ok = pipeline.ok
            details: dict[str, Any] = {
                "plan_source": pipeline.plan_source,
                "plan_error": pipeline.plan_error,
                "solver_steps": pipeline.solver_steps,
                "total_calls": pipeline.total_calls,
                "total_tokens": pipeline.total_tokens,
                "verifier_mode": pipeline.verifier_mode,
                "verifier_status": verifier_status,
                "verifier_changed_answer": pipeline.verifier_changed_answer,
                "verifier_mismatch": pipeline.verifier_mismatch,
                "verifier_audit_text": verifier_text,
                "answer_pre_verify": pipeline.answer_pre_verify,
                "synthesis_draft": synthesis_draft,
                "stall_count": pipeline.stall_count,
            }
        else:
            result = await run_worker(
                llm=llm_func,
                runtime=runtime,
                system=system,
                task=query + ANSWER_HINT,
                max_steps=_int_env("MAX_TOTAL_STEPS", 20),
                emit=emit,
            )
            final_text = result.final_text
            ok = result.ok
            details = {
                "steps": result.steps,
                "executions": result.executions,
                "first_attempt_ok": result.first_attempt_ok,
                "verifier_status": "not_run",
                "verifier_audit_text": "",
                "answer_pre_verify": None,
                "synthesis_draft": "",
            }

        usage_after = snapshot_usage()
        charts = list(getattr(runtime.ns.get("charts"), "charts", []) or [])
        return {
            "arm": arm,
            "query": query,
            "answer": runtime.ns.get("ANSWER"),
            "final_text": final_text,
            "charts": charts,
            "ok": ok,
            "seconds": round(time.perf_counter() - started, 3),
            "usage": _usage_delta(usage_before, usage_after),
            **details,
        }
    finally:
        guard.close_if_idle(runtime)


async def _stream_chat(
    *,
    request: Request,
    query: str,
    arm: Literal["single", "multi"],
    runtime_factory: RuntimeFactory,
    llm_func: LLMFunc,
    verifier_mode: str,
    guard: RuntimeGuard,
    timeout_seconds: float,
    heartbeat_seconds: float,
):
    """Yield SSE events while a producer task runs the agent."""
    queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()

    def emit_ui(kind: str, payload: Any) -> None:
        queue.put_nowait((kind, payload))

    def emit_agent(kind: str, payload: Any) -> None:
        # run_worker/run_pipeline also emit a textual "final" event. Keep that
        # as trace evidence and reserve the public "final" event for the result
        # object the UI renders (answer, charts, usage).
        if kind == "final":
            kind = "agent_final"
        queue.put_nowait((kind, payload))

    async def produce() -> None:
        started = time.perf_counter()
        emit_ui(
            "start",
            {
                "arm": arm,
                "model": os.environ.get("LLM_MODEL"),
                "query": query,
                "timeout_seconds": timeout_seconds,
            },
        )
        try:
            result = await asyncio.wait_for(
                _run_chat(
                    query=query,
                    arm=arm,
                    runtime_factory=runtime_factory,
                    llm_func=llm_func,
                    verifier_mode=verifier_mode,
                    guard=guard,
                    emit=emit_agent,
                ),
                timeout=timeout_seconds,
            )
            emit_ui("final", result)
            emit_ui("done", {"ok": bool(result.get("ok")), "seconds": result.get("seconds")})
        except asyncio.TimeoutError:
            poisoned = guard.observe()
            message = f"Timed out after {timeout_seconds:g} s"
            if poisoned:
                message += "; runtime still executing, restart the server"
            emit_ui(
                "error",
                {
                    "type": "TimeoutError",
                    "message": message,
                },
            )
            emit_ui("done", {"ok": False, "seconds": round(time.perf_counter() - started, 3)})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - stream the failure to the UI
            emit_ui("error", {"type": type(exc).__name__, "message": _safe_error(exc)})
            emit_ui("done", {"ok": False, "seconds": round(time.perf_counter() - started, 3)})
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(produce())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except asyncio.TimeoutError:
                if await request.is_disconnected():
                    break
                yield ": keep-alive\n\n"
                continue

            if item is None:
                break
            kind, payload = item
            yield _sse(kind, payload)
            if await request.is_disconnected():
                break
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        # A client can disconnect while a worker thread is still executing.
        guard.observe()


def build_reference_payload() -> dict[str, Any]:
    """Load the committed deterministic reference and attach chart configs.

    This is deliberately file-only: no model call and no database connection.
    The browser can therefore show the paper-target reference even when the
    live agent fails.
    """
    payload = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))
    observed = payload.get("observed") or {}
    anomaly = payload.get("anomaly_assessment") or {}

    charts = EChartsConfigManager()
    charts.bar(
        "Matched-hour NO2: event vs preceding 14 days",
        ["14-day mean", "14-day max", "Event 20:00"],
        {
            "NO2": [
                anomaly.get("matched_hour_mean"),
                anomaly.get("matched_hour_max_prev_14d"),
                observed.get("no2_at_2000"),
            ]
        },
        "NO2 (ug/m3)",
    )
    charts.bar(
        "PM2.5 evening rise: Tai Po vs other 17",
        ["Tai Po", "Other 17 mean"],
        {
            "PM2.5 rise": [
                anomaly.get("nearest_station_pm25_rise"),
                anomaly.get("network_pm25_mean_rise_other_17"),
            ]
        },
        "PM2.5 rise (ug/m3)",
    )
    payload["charts"] = charts.charts
    return payload


def create_app(
    *,
    runtime_factory: RuntimeFactory = build_runtime,
    llm_func: LLMFunc = llm,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    verifier_mode: str = "overwrite",
    preview_banner: bool = False,
) -> FastAPI:
    """Build the app; the keyword arguments are the fake-LLM test seam."""
    load_agent_env(scrub=True)

    app = FastAPI(
        title="Neuro-Air Lite",
        description="Hong Kong air-quality analyst: single or multi-agent, SSE streaming.",
        version="0.1.0",
    )
    app.state.gate = BusyGate()
    app.state.guard = RuntimeGuard()
    app.state.runtime_factory = runtime_factory
    app.state.llm_func = llm_func
    app.state.timeout_seconds = timeout_seconds
    app.state.verifier_mode = verifier_mode

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False, response_model=None)
    async def index() -> FileResponse | HTMLResponse:
        if preview_banner:
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            banner = (
                '<div class="preview-banner">SYNTHETIC DEMO DATA &mdash; '
                "mock LLM, no live database run</div>"
            )
            return HTMLResponse(html.replace("<body>", "<body>\n  " + banner, 1))
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        degraded = app.state.guard.poisoned
        return {
            "status": "degraded" if degraded else "ok",
            "degraded": degraded,
            "busy": app.state.gate.busy,
            "arm_default": "single",
            "arms": ["single", "multi"],
            "model": os.environ.get("LLM_MODEL"),
            "timeout_seconds": timeout_seconds,
        }

    @app.get("/api/reference/tai_po")
    async def reference_tai_po() -> JSONResponse:
        try:
            return JSONResponse(build_reference_payload())
        except (OSError, json.JSONDecodeError) as error:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"error": f"reference snapshot unavailable: {error}"},
            )

    @app.post("/api/chat", response_model=None)
    async def chat(payload: ChatRequest, request: Request) -> StreamingResponse | JSONResponse:
        if app.state.guard.poisoned:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"error": "runtime still executing; restart the server"},
            )
        lease = app.state.gate.try_acquire()
        if lease is None:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Another query is already running. Try again when it finishes.",
                headers={"Retry-After": "2"},
            )
        try:
            response = StreamingResponse(
                _stream_chat(
                    request=request,
                    query=payload.query,
                    arm=payload.arm,
                    runtime_factory=app.state.runtime_factory,
                    llm_func=app.state.llm_func,
                    verifier_mode=app.state.verifier_mode,
                    guard=app.state.guard,
                    timeout_seconds=app.state.timeout_seconds,
                    heartbeat_seconds=heartbeat_seconds,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
                background=BackgroundTask(lease.release),
            )
            return response
        except Exception:
            lease.release()
            raise

    return app


app = create_app()
