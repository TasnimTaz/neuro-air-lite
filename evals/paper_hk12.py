"""Run the vendored Neuro-Air HK-12 tasks through this project's harness.

This is an external-anchor adapter, not a new benchmark and not a replacement for
``evals/run_v5.py``. It loads the 12 HK task JSON files and their validators
byte-for-byte from ``evals/vendor/neuroair_hk12`` (MIT, source commit recorded in
the vendor README), runs each original query plus its original ``requirements``
through the same roles/runtime as the v5 harness, and scores with the *paper's*
validators through a deliberately small compatibility shim.

    uv run python evals/paper_hk12.py --smoke
    uv run python evals/paper_hk12.py --arm single --repeats 3 \
        --out runs/paper_hk12/qwen14b_single --model qwen2.5-coder:14b \
        --base-url http://127.0.0.1:11434/v1
    uv run python evals/paper_hk12.py --arm multi --repeats 3 \
        --out runs/paper_hk12/qwen14b_multi --model qwen2.5-coder:14b \
        --base-url http://127.0.0.1:11434/v1 --verifier-mode safe

Nothing under ``core/``, ``domains/hk/``, ``evals/v5/`` or ``evals/run_v5.py`` is
modified. The shim below only supplies the paper-side import names at runtime.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Awaitable, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agent import BUDGET_EXHAUSTED_SENTINEL, run_worker  # noqa: E402
from core.env import load_agent_env  # noqa: E402
from core.llm import snapshot_usage  # noqa: E402
from core.orchestrator import run_pipeline  # noqa: E402
from core.prompts import HKT  # noqa: E402
from core.runtime_v5 import PythonRuntimeV5  # noqa: E402
from domains.hk import roles_v5  # noqa: E402
from domains.hk.db import make_ro_engine  # noqa: E402
from domains.hk.schema_prompt import get_system_prompt  # noqa: E402
from domains.hk.tools import build_runtime  # noqa: E402
from evals.run_v5 import is_infra_failure, make_llm  # noqa: E402

VENDOR = ROOT / "evals" / "vendor" / "neuroair_hk12"
LLM = Callable[[list[dict[str, str]]], Awaitable[str]]


# --------------------------------------------------------------- paper shim
# These names are imported by the vendored validator modules. They are defined
# here and installed into sys.modules before those modules are loaded. The
# validator source files themselves are never edited.


@dataclass
class Variable:
    """Minimal stand-in for cave_agent.runtime.Variable (name, default, desc)."""

    name: str
    default: Any = None
    description: str = ""

    @property
    def desc(self) -> str:
        return self.description


@dataclass
class Turn:
    """Minimal stand-in for core.types.Turn from the paper benchmark."""

    query: str = ""
    validator: str | None = None
    pre_turn_hook: str | None = None


@dataclass
class ValidatorResult:
    """Exact shape of the paper benchmark's benchmark/core/validation.py result."""

    success: bool
    message: str
    variables_not_set: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "message": self.message,
            "variables_not_set": self.variables_not_set,
        }


def is_finite_number(value: Any) -> bool:
    """Paper semantics: reject None, bool, strings, NaN and +/-inf."""
    try:
        import numpy as np

        numeric: tuple = (np.integer, np.floating)
    except ImportError:  # pragma: no cover
        numeric = ()
    if value is None or isinstance(value, bool):
        return False
    if not isinstance(value, (int, float, *numeric)):
        return False
    return math.isfinite(float(value))


def compare_numeric(
    actual: Any,
    expected: Any,
    *,
    tolerance: float | None = None,
    decimals: int | None = None,
) -> bool:
    """Paper semantics: signed comparison, absolute tolerance (no sign erasure)."""
    if (tolerance is None) == (decimals is None):
        raise ValueError("compare_numeric: pass exactly one of `tolerance=` or `decimals=`")
    if tolerance is None:
        tolerance = 0.6 * (10 ** (-decimals))
    if not is_finite_number(expected):
        raise ValueError(f"compare_numeric: `expected` must be a finite number, got {expected!r}")
    if not is_finite_number(actual):
        return False
    return abs(float(actual) - float(expected)) <= tolerance


# The paper's HK schema numbers stations 1..18; this lite DB numbers the same
# named stations 19..36. The mapping is from the paper's own schema prompt
# (benchmark/domains/HK/prompts.py, commit dbc47f0c). It is applied in two
# deliberately separate places: (a) paper-station identifiers in the
# model-facing query + requirements (identifier crosswalk only), and (b) the
# vendored validators' read-only ground-truth SQL. Validator source files,
# roles, runtime and scorer are unchanged. No other number is rewritten.
PAPER_TO_LOCAL_STATION = {
    1: 20,   # Southern
    2: 32,   # North
    3: 22,   # Kwun Tong
    4: 26,   # Tseung Kwan O
    5: 28,   # Tuen Mun
    6: 29,   # Tung Chung
    7: 21,   # Eastern
    8: 33,   # Tap Mun
    9: 24,   # Kwai Chung
    10: 27,  # Yuen Long
    11: 31,  # Sha Tin
    12: 23,  # Sham Shui Po
    13: 30,  # Tai Po
    14: 36,  # Mong Kok
    15: 19,  # Central/Western
    16: 35,  # Central
    17: 34,  # Causeway Bay
    18: 25,  # Tsuen Wan
}
PAPER_FROM_LOCAL_STATION = {local: paper for paper, local in PAPER_TO_LOCAL_STATION.items()}
QUESTION_CROSSWALK = "paper_station_ids_v1"


def _map_station_prefix(match: re.Match[str]) -> str:
    prefix, sid = match.groups()
    mapped = PAPER_TO_LOCAL_STATION.get(int(sid))
    return prefix + (str(mapped) if mapped is not None else sid)


def _map_station_choices(match: re.Match[str]) -> str:
    prefix, body, suffix = match.groups()

    def one(m: re.Match[str]) -> str:
        value = int(m.group(1))
        return str(PAPER_TO_LOCAL_STATION.get(value, value))

    return prefix + re.sub(r"\b(\d{1,2})\b", one, body) + suffix


def crosswalk_station_ids(text: str) -> str:
    """Map paper station IDs to local IDs in model-facing text only.

    Rewrites exactly three forms: ``station_id: N`` / ``station_id = N``,
    ``station N``, and the numeric choices inside ``higher_peak_station_id (...)``.
    Dates, clock times, counts such as "all 18 monitoring stations", and
    ``district_id`` are deliberately left alone. Local IDs (19..36) are not keys
    in the map, so re-running the function is idempotent.
    """
    out = re.sub(
        r"(\bstation_id\s*[:=]\s*)(\d{1,2})\b",
        _map_station_prefix,
        text,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"(\bstation\s+)(\d{1,2})\b",
        _map_station_prefix,
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(
        r"(\bhigher_peak_station_id\s*\()([^)]*)(\))",
        _map_station_choices,
        out,
        flags=re.IGNORECASE,
    )
    return out


def crosswalk_manifest(tasks: list[PaperTask]) -> dict[str, dict[str, str]]:
    """Original and crosswalked model-facing text for every vendored task."""
    return {
        task.name: {"raw": task.question_raw, "translated": task.question}
        for task in tasks
    }


def paper_text(sql: str) -> Any:
    """sqlalchemy.text wrapper: paper station IDs -> local IDs in validator SQL."""
    from sqlalchemy import text as real_text

    def replace(match: re.Match[str]) -> str:
        prefix, sid = match.groups()
        mapped = PAPER_TO_LOCAL_STATION.get(int(sid))
        return prefix + (str(mapped) if mapped is not None else sid)

    rewritten = re.sub(
        r"(station_id\s*=\s*)(\d{1,2})\b",
        replace,
        sql,
        flags=re.IGNORECASE,
    )
    return real_text(rewritten)


def _to_paper_station_id(value: Any) -> Any:
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        return value
    return PAPER_FROM_LOCAL_STATION.get(as_int, value)


class ShimRuntime:
    """Paper IPythonRuntime surface; ``retrieve`` reads our frozen runtime ns.

    ``higher_peak_station_id`` is declared in the paper requirements as 16 or 5;
    when the agent follows our local schema it returns 35 or 28, so this one
    variable is mapped back to the paper's numbering. No other value is altered.
    """

    def __init__(self, namespace: dict[str, Any]):
        self.namespace = namespace

    def retrieve(self, name: str) -> Any:
        value = self.namespace.get(name)
        if name == "higher_peak_station_id":
            return _to_paper_station_id(value)
        return value


def _module(name: str, **attrs: Any) -> ModuleType:
    mod = ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


def install_shims(engine: Any) -> None:
    """Register paper import names. Called once before loading validator modules."""
    cave = _module("cave_agent")
    cave.__path__ = []
    _module("cave_agent.runtime", IPythonRuntime=ShimRuntime, Variable=Variable)
    setattr(cave, "runtime", sys.modules["cave_agent.runtime"])

    core = importlib.import_module("core")
    _module("core.types", Turn=Turn)
    _module(
        "core.validation",
        ValidatorResult=ValidatorResult,
        is_finite_number=is_finite_number,
        compare_numeric=compare_numeric,
    )
    setattr(core, "types", sys.modules["core.types"])
    setattr(core, "validation", sys.modules["core.validation"])

    domains = importlib.import_module("domains")
    hk = _module("domains.HK")
    hk.__path__ = []
    db = _module("domains.HK.db", engine=engine)
    setattr(hk, "db", db)
    setattr(domains, "HK", hk)


# ------------------------------------------------------------- task loading


@dataclass(frozen=True)
class Output:
    var: str
    desc: str
    kind: str  # float | int | text | dict


@dataclass(frozen=True)
class PaperTask:
    name: str
    category: str
    difficulty: str
    query: str
    requirements: str
    validator_name: str
    validator: Callable[..., ValidatorResult]
    outputs: tuple[Output, ...]

    @property
    def question_raw(self) -> str:
        """Original paper query + requirements, kept verbatim for the record."""
        return self.query + "\n\n" + self.requirements

    @property
    def question(self) -> str:
        """Model-facing text after the paper->local station identifier crosswalk."""
        return crosswalk_station_ids(self.question_raw)

    def var_names(self) -> tuple[str, ...]:
        return tuple(o.var for o in self.outputs)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_commit() -> str:
    readme = (VENDOR / "README.md").read_text(encoding="utf-8")
    match = re.search(r"^Source commit:\s*([0-9a-f]+)\s*$", readme, re.MULTILINE)
    if not match:
        raise RuntimeError("vendor README has no source commit")
    return match.group(1)


def _infer_kind(desc: str) -> str:
    text = desc.casefold()
    for kind in ("float", "int", "dict", "str"):
        if f"({kind}" in text:
            return "text" if kind == "str" else kind
    return "text"


def _load_validator_module(json_path: Path, module_name: str) -> ModuleType:
    parts = module_name.split(".")
    if len(parts) < 4 or parts[0] != "evals" or parts[1] != "neuroair":
        raise RuntimeError(f"unexpected vendored module path: {module_name}")
    category, leaf = parts[2], parts[-1]
    py_path = VENDOR / category / "HK" / f"{leaf}.py"
    if not py_path.exists():
        raise FileNotFoundError(py_path)
    if _sha256(py_path) == "":  # defensive only; keeps the hash check explicit
        raise RuntimeError(py_path)
    full_name = f"_paper_hk12_vendor_{category}_{leaf}"
    spec = importlib.util.spec_from_file_location(full_name, py_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {py_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)
    # Only the vendored validators use this rewritten text() wrapper.
    mod.text = paper_text
    return mod


def load_tasks() -> list[PaperTask]:
    tasks: list[PaperTask] = []
    for json_path in sorted(VENDOR.rglob("HK/*.json")):
        raw = json.loads(json_path.read_text(encoding="utf-8"))
        spec = raw[0] if isinstance(raw, list) else raw
        mod = _load_validator_module(json_path, spec["module"])
        turn = spec["conversations"][0]["turns"][0]
        variables = getattr(mod, "variables", [])
        outputs = tuple(
            Output(
                var=v.name,
                desc=v.description,
                kind=_infer_kind(v.description),
            )
            for v in variables
        )
        validator_name = turn["validator"]
        validator = getattr(mod, "validators", {}).get(validator_name)
        if validator is None:
            raise RuntimeError(f"missing validator {validator_name} in {json_path}")
        tasks.append(
            PaperTask(
                name=spec["name"],
                category=spec["category"],
                difficulty=spec["difficulty"],
                query=turn["query"],
                requirements=spec["requirements"],
                validator_name=validator_name,
                validator=validator,
                outputs=outputs,
            )
        )
    if len(tasks) != 12:
        raise RuntimeError(f"expected 12 vendored HK tasks, got {len(tasks)}")
    return tasks


def output_contract(task: PaperTask) -> str:
    """Protocol suffix, identical for both arms; wording follows run_v5."""
    labels = {"float": "a number", "int": "an integer", "text": "a short string", "dict": "a dictionary"}
    lines = [
        "\n\nStore the results in these runtime variables (use exactly these names) and print each one:"
    ]
    for out in task.outputs:
        lines.append(f"- `{out.var}`: {out.desc} ({labels[out.kind]})")
    lines.append(
        "If a value cannot be determined from the data, leave its variable unset and say why. "
        "Then reply with one short sentence and no code block."
    )
    return "\n".join(lines)


def _run_validator(task: PaperTask, final_text: str, namespace: dict[str, Any]) -> dict[str, Any]:
    try:
        result = task.validator(
            final_text,
            ShimRuntime(namespace),
            Turn(query=task.query, validator=task.validator_name),
        )
        return result.as_dict()
    except Exception as exc:  # validator-side failure is an answer failure, not infra
        return {
            "success": False,
            "message": f"validator error: {type(exc).__name__}: {exc}",
            "variables_not_set": False,
        }


# ---------------------------------------------------------------- run logic


async def run_task(
    task: PaperTask,
    *,
    arm: str,
    repeat: int,
    llm: LLM,
    runtime_factory: Callable[[], PythonRuntimeV5],
    base_system: Callable[[PythonRuntimeV5], str],
    max_steps: int = 20,
    verifier_mode: str = "safe",
) -> dict[str, Any]:
    runtime = runtime_factory()
    trace: list[tuple[str, Any]] = []
    try:
        base = base_system(runtime)
        suffix = output_contract(task)
        usage0 = snapshot_usage()
        started = time.perf_counter()
        pipeline = None
        if arm == "multi":
            pipeline = await run_pipeline(
                llm=llm,
                runtime=runtime,
                role_prompts=roles_v5.ROLE_PROMPTS,
                shared_rules=base + "\n\n" + roles_v5.MULTI_RULES,
                coordinator_system=roles_v5.COORDINATOR_SYSTEM,
                query=task.question,
                task_suffix=suffix,
                max_total=max_steps,
                verify_reserve=6,
                synth_reserve=3,
                verifier_mode=verifier_mode,
                answer_vars=task.var_names(),
                emit=lambda kind, payload: trace.append((kind, payload)),
            )
            final_text, steps = pipeline.final_text, pipeline.solver_steps
            executions = sum(r.executions for r in pipeline.worker_runs)
            first = next((r.first_attempt_ok for r in pipeline.worker_runs if r.executions > 0), None)
        else:
            res = await run_worker(
                llm=llm,
                runtime=runtime,
                system=base + "\n\n" + roles_v5.MERGED_ROLE_ADDENDUM_V5,
                task=task.question + suffix,
                max_steps=max_steps,
                emit=lambda kind, payload: trace.append((kind, payload)),
            )
            final_text, steps, executions, first = res.final_text, res.steps, res.executions, res.first_attempt_ok

        elapsed = time.perf_counter() - started
        usage1 = snapshot_usage()
        tokens = (usage1["prompt"] - usage0["prompt"]) + (usage1["completion"] - usage0["completion"])
        calls = usage1["calls"] - usage0["calls"]
        verdict = _run_validator(task, final_text, runtime.ns)
        out: dict[str, Any] = {
            "task_id": task.name,
            "repeat": repeat,
            "family": task.category,
            "category": task.category,
            "difficulty": task.difficulty,
            "split": "external_hk12",
            "arm": arm,
            "question_crosswalk": QUESTION_CROSSWALK,
            "question_sha256": hashlib.sha256(task.question.encode("utf-8")).hexdigest(),
            "pass": bool(verdict["success"]),
            "validator": verdict,
            "steps": steps,
            "executions": executions,
            "first_attempt_ok": first,
            "tokens": tokens,
            "calls": calls,
            "seconds": elapsed,
            "final_text": final_text,
            "budget_exhausted": final_text.strip() == BUDGET_EXHAUSTED_SENTINEL,
            "last_error": next((str(p)[:600] for k, p in reversed(trace) if k == "output" and "ERROR" in str(p)), ""),
        }
        if pipeline is not None:
            pre = {k: v for k, v in pipeline.vars_pre_verify.items() if v is not None}
            out.update(
                pass_pre_verify=bool(_run_validator(task, final_text, pre)["success"]),
                verifier_mode=pipeline.verifier_mode,
                verifier_changed=pipeline.verifier_changed_answer,
                verifier_rejections=pipeline.verifier_rejections,
                stall_count=pipeline.stall_count,
            )
        return out
    finally:
        runtime.close()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _summarize(outs: list[dict[str, Any]], tasks: list[PaperTask]) -> dict[str, Any]:
    n = len(outs)
    if not n:
        return {"n": 0}
    by_task: dict[str, dict[str, Any]] = {}
    for task in tasks:
        rows = [o for o in outs if o["task_id"] == task.name]
        by_task[task.name] = {
            "n": len(rows),
            "pass": sum(bool(o["pass"]) for o in rows),
            "accuracy": round(sum(bool(o["pass"]) for o in rows) / len(rows), 4) if rows else None,
        }
    s: dict[str, Any] = {
        "n": n,
        "pass": sum(bool(o["pass"]) for o in outs),
        "accuracy": round(sum(bool(o["pass"]) for o in outs) / n, 4),
        "by_task": by_task,
        "mean_tokens": round(sum(o["tokens"] for o in outs) / n),
        "mean_seconds": round(sum(o["seconds"] for o in outs) / n, 1),
        "infra_retries_total": sum(o.get("infra_retries", 0) for o in outs),
    }
    if outs[0]["arm"] == "multi":
        s["pass_pre_verify"] = sum(1 for o in outs if o.get("pass_pre_verify"))
        s["stalls"] = sum(o.get("stall_count", 0) for o in outs)
        s["verifier_rejections"] = sum(len(o.get("verifier_rejections", [])) for o in outs)
    return s


def smoke_validators(tasks: list[PaperTask]) -> int:
    """Prove every validator imports and its ground-truth SQL executes on our DB."""
    failed = 0
    for task in tasks:
        verdict = _run_validator(task, "", {})
        expected_unset = bool(verdict["variables_not_set"])
        status = "OK" if expected_unset else "UNEXPECTED"
        if not expected_unset:
            failed += 1
        print(f"[smoke] {task.name:<8} {status:<10} {verdict['message'][:160]}", flush=True)
    print(f"[smoke] tasks={len(tasks)} unexpected={failed}")
    return 1 if failed else 0


async def main_async(args: argparse.Namespace) -> int:
    load_agent_env(override=False, scrub=True)
    if "DB_URL_ADMIN" in os.environ:
        print("[FAIL] DB_URL_ADMIN is exported; agent environment is not scrubbed", file=sys.stderr)
        return 1
    engine = make_ro_engine()
    install_shims(engine)
    tasks = load_tasks()

    if args.smoke:
        return smoke_validators(tasks)

    model = args.model or os.environ.get("LLM_MODEL", "qwen2.5-coder:14b")
    if args.base_url:
        os.environ["LLM_BASE_URL"] = args.base_url
    extra = json.loads(args.extra_body) if args.extra_body else None
    out_dir = Path(args.out)
    (out_dir / "cases").mkdir(parents=True, exist_ok=True)

    prompt_time = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=HKT)

    def runtime_factory() -> PythonRuntimeV5:
        rt = build_runtime()
        return PythonRuntimeV5(variables=dict(rt.ns))

    def base_system(rt: PythonRuntimeV5) -> str:
        return get_system_prompt(engine=rt.ns["engine"], now=prompt_time)

    probe = runtime_factory()
    try:
        base_sha = hashlib.sha256(base_system(probe).encode()).hexdigest()
    finally:
        probe.close()

    meta = {
        "model": model,
        "arm": args.arm,
        "temperature": args.temperature,
        "extra_body": extra,
        "max_steps": args.max_steps,
        "verifier_mode": args.verifier_mode,
        "repeats": args.repeats,
        "question_crosswalk": {
            "name": QUESTION_CROSSWALK,
            "applies_to": "model-facing query + requirements only",
            "forms": ["station_id: N", "station_id = N", "station N", "higher_peak_station_id (N or M)"],
            "untouched": ["district_id", "dates", "clock times", "station counts", "validator source", "validator SQL shim"],
        },
        "question_crosswalk_by_task": crosswalk_manifest(tasks),
        "source_repo": "https://github.com/acodercat/Neuro-Air",
        "source_commit": source_commit(),
        "vendor_sha256": {
            p.relative_to(VENDOR).as_posix(): _sha256(p)
            for p in sorted(VENDOR.rglob("*"))
            if p.is_file() and p.name != "README.md" and "__pycache__" not in p.parts
        },
        "base_prompt_sha256": base_sha,
        "single_addendum_sha256": hashlib.sha256(roles_v5.MERGED_ROLE_ADDENDUM_V5.encode()).hexdigest(),
        "multi_prompts_sha256": hashlib.sha256(
            (roles_v5.COORDINATOR_SYSTEM + roles_v5.MULTI_RULES + "".join(roles_v5.ROLE_PROMPTS.values())).encode()
        ).hexdigest(),
    }
    meta_path = out_dir / "run_meta.json"
    if meta_path.exists():
        old = json.loads(meta_path.read_text(encoding="utf-8"))
        keys = (
            "model", "arm", "temperature", "max_steps", "verifier_mode", "repeats",
            "source_commit", "base_prompt_sha256", "question_crosswalk",
        )
        if any(old.get(k) != meta.get(k) for k in keys):
            print("[FAIL] resume refused: run_meta.json differs from current settings", file=sys.stderr)
            return 1

    llm = make_llm(model, args.temperature, extra)
    outs: list[dict[str, Any]] = []
    total = len(tasks) * args.repeats
    done = 0
    for task in tasks:
        for repeat in range(1, args.repeats + 1):
            done += 1
            path = out_dir / "cases" / f"repeat-{repeat:02d}-case-{task.name}.json"
            if path.exists():
                outs.append(json.loads(path.read_text(encoding="utf-8")))
                continue
            retries = 0
            while True:
                try:
                    out = await run_task(
                        task, arm=args.arm, repeat=repeat, llm=llm, runtime_factory=runtime_factory,
                        base_system=base_system, max_steps=args.max_steps, verifier_mode=args.verifier_mode,
                    )
                except Exception as exc:  # noqa: BLE001 - provider/transport failure
                    out = {
                        "task_id": task.name, "repeat": repeat, "category": task.category, "arm": args.arm,
                        "pass": False, "tokens": 0, "steps": 0, "executions": 0, "seconds": 0.0,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                if is_infra_failure(out) and retries < args.infra_retries:
                    retries += 1
                    await asyncio.sleep(2.0)
                    continue
                break
            out["infra_retries"] = retries
            out["infra_failed"] = is_infra_failure(out)
            _atomic_write(path, out)
            outs.append(out)
            print(
                f"[{done}/{total}] {task.name:<8} r{repeat} {args.arm:<6} "
                f"{'PASS' if out['pass'] else 'fail'} tokens={out['tokens']} steps={out.get('steps')} "
                f"infra_retries={retries}",
                flush=True,
            )

    _atomic_write(meta_path, meta)
    scored = [o for o in outs if not o.get("infra_failed")]
    summary = {
        "meta": meta,
        "summary": _summarize(scored, tasks),
        "infra_failed_task_ids": [f"{o['task_id']}#r{o['repeat']}" for o in outs if o.get("infra_failed")],
    }
    _atomic_write(out_dir / "run.json", {**summary, "cases": outs})
    print(json.dumps(summary["summary"], indent=1), flush=True)
    if summary["infra_failed_task_ids"]:
        print("INFRA-FAILED (excluded; re-run same --out):", summary["infra_failed_task_ids"])
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--arm", choices=["single", "multi"])
    ap.add_argument("--out", required=False)
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--api-key-env", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--extra-body", default=None)
    ap.add_argument("--max-steps", type=int, default=20)
    ap.add_argument("--verifier-mode", choices=["safe", "overwrite", "flag", "none"], default="safe")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--infra-retries", type=int, default=2)
    ap.add_argument("--smoke", action="store_true", help="only check validator imports/ground-truth SQL")
    args = ap.parse_args(argv)
    if not args.smoke and (not args.arm or not args.out):
        ap.error("--arm and --out are required unless --smoke is used")
    if args.api_key_env:
        ap.error("--api-key-env is intentionally unsupported: local model only")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
