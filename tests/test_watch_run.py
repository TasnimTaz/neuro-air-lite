from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import watch_run  # noqa: E402


def write_case(run_dir: Path, name: str, *, tokens: int = 100, seconds: float = 1.0,
               ok: bool = True, mtime: float | None = None) -> Path:
    cases = run_dir / "cases"
    cases.mkdir(parents=True, exist_ok=True)
    path = cases / name
    path.write_text(json.dumps({
        "tokens": tokens,
        "seconds": seconds,
        "executions": 1,
        "ok": ok,
    }), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def patch_loop(monkeypatch, alive_values, *, now=None):
    alive_iter = iter(alive_values)
    monkeypatch.setattr(watch_run, "alive", lambda pid: next(alive_iter, False))
    monkeypatch.setattr(watch_run.time, "sleep", lambda seconds: None)
    if now is not None:
        if isinstance(now, list):
            now_iter = iter(now)
            monkeypatch.setattr(watch_run.time, "time", lambda: next(now_iter, now[-1]))
        else:
            monkeypatch.setattr(watch_run.time, "time", lambda: now)


def record_kill(monkeypatch):
    calls = []
    monkeypatch.setattr(watch_run, "kill_tree", lambda pid: calls.append(pid))
    return calls


def test_scan_only_reads_per_case_files(tmp_path):
    run_dir = tmp_path / "run"
    write_case(run_dir, "repeat-01-case-01.json", tokens=120, seconds=3.0, ok=True)

    (run_dir).mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "tokens": 999_000_000, "executions": 999, "cases": 1,
    }), encoding="utf-8")
    (run_dir / "run_meta.json").write_text(json.dumps({
        "tokens": 999_000_000, "executions": 999,
    }), encoding="utf-8")
    (run_dir / "repeat-01-case-99.json").write_text(json.dumps({
        "tokens": 999_000_000, "executions": 999,
    }), encoding="utf-8")
    write_case(run_dir, "not-a-case.json", tokens=999_000_000, ok=False)

    result = watch_run.scan(run_dir)
    assert result["cases"] == 1
    assert result["tokens"] == 120
    assert result["worst_case_seconds"] == 3.0
    assert result["not_ok_cases"] == 0


def test_v5_task_case_files_are_scanned(tmp_path):
    run_dir = tmp_path / "run"
    write_case(run_dir, "task-0007.json", tokens=321, seconds=5.0)

    result = watch_run.scan(run_dir)
    assert result["cases"] == 1
    assert result["tokens"] == 321
    assert result["worst_case_seconds"] == 5.0


def test_total_token_abort_kills_process(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    write_case(run_dir, "repeat-01-case-01.json", tokens=1200, seconds=2.0)
    patch_loop(monkeypatch, [True])
    killed = record_kill(monkeypatch)

    code = watch_run.main([
        "--run-dir", str(run_dir), "--pid", "4321", "--max-tokens", "1000",
    ])
    out = capsys.readouterr().out
    assert code == 2
    assert "ABORT: total tokens 1,200 > 1,000" in out
    assert killed == [4321]


def test_one_case_token_abort(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    write_case(run_dir, "repeat-01-case-01.json", tokens=600, seconds=1.0)
    patch_loop(monkeypatch, [True])
    record_kill(monkeypatch)

    code = watch_run.main([
        "--run-dir", str(run_dir), "--pid", "1", "--max-tokens", "10_000",
        "--max-case-tokens", "500",
    ])
    assert code == 2
    assert "one case used" in capsys.readouterr().out


def test_one_case_seconds_abort(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    write_case(run_dir, "repeat-01-case-01.json", tokens=100, seconds=20.0)
    patch_loop(monkeypatch, [True])
    record_kill(monkeypatch)

    code = watch_run.main([
        "--run-dir", str(run_dir), "--pid", "1", "--max-tokens", "10_000",
        "--max-case-seconds", "10",
    ])
    assert code == 2
    assert "one case took" in capsys.readouterr().out


def test_idle_abort_catches_hung_case(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    write_case(run_dir, "repeat-01-case-01.json", tokens=100, seconds=1.0, mtime=500)
    patch_loop(monkeypatch, [True], now=[1000.0, 2000.0])
    killed = record_kill(monkeypatch)

    code = watch_run.main([
        "--run-dir", str(run_dir), "--pid", "9", "--max-tokens", "10_000",
        "--max-idle-seconds", "30",
    ])
    out = capsys.readouterr().out
    assert code == 2
    assert "no new case file" in out
    assert killed == [9]


def test_not_ok_abort_is_off_by_default(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    write_case(run_dir, "repeat-01-case-01.json", tokens=100, ok=False)
    patch_loop(monkeypatch, [True, False], now=1000.0)
    killed = record_kill(monkeypatch)

    code = watch_run.main([
        "--run-dir", str(run_dir), "--pid", "1", "--max-tokens", "10_000",
    ])
    assert code == 0
    assert "run process ended" in capsys.readouterr().out
    assert killed == []


def test_not_ok_abort_can_be_enabled(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    write_case(run_dir, "repeat-01-case-01.json", tokens=100, ok=False)
    patch_loop(monkeypatch, [True])
    record_kill(monkeypatch)

    code = watch_run.main([
        "--run-dir", str(run_dir), "--pid", "1", "--max-tokens", "10_000",
        "--max-not-ok", "1",
    ])
    assert code == 2
    assert "did not finish ok" in capsys.readouterr().out


def test_healthy_run_exits_zero(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    write_case(run_dir, "repeat-01-case-01.json", tokens=100, seconds=1.0, mtime=1000)
    patch_loop(monkeypatch, [True, False], now=1000.0)
    killed = record_kill(monkeypatch)

    code = watch_run.main([
        "--run-dir", str(run_dir), "--pid", "1", "--max-tokens", "10_000",
    ])
    assert code == 0
    assert "cases=1" in capsys.readouterr().out
    assert killed == []
