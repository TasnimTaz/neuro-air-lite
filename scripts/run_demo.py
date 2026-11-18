"""Run the live API/SSE demo and save inspectable transcripts.

The browser UI consumes the same ``POST /api/chat`` Server-Sent Events stream
that this script records. Start the API first:

    uv run uvicorn api.app:app --host 127.0.0.1 --port 8000

Then:

    uv run python scripts/run_demo.py \
        --base-url http://127.0.0.1:8000 \
        --out runs/demo/day8_live

The first four queries in ``scripts/demo_queries.md`` run on the single arm;
the Tai Po case study runs on the multi arm. Each transcript stores the raw SSE
bytes, parsed events, final payload, chart configs, verifier status and any
error. Artifacts are gitignored under ``runs/``.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUERIES = ROOT / "scripts" / "demo_queries.md"
DEFAULT_OUT = ROOT / "runs" / "demo" / "day8_live"


def load_queries(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    queries = [match.strip() for match in re.findall(r"`([^`]+)`", text)]
    # The markdown also has inline code in the "what to inspect" section. The
    # demo queries are the five long, question-shaped spans.
    queries = [query for query in queries if query.endswith(("?", "."))]
    if len(queries) < 5:
        raise SystemExit(f"expected five demo queries in {path}, found {len(queries)}")
    return queries[:5]


def parse_sse(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    name = "message"
    data_lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line == "":
            if data_lines:
                payload_text = "\n".join(data_lines)
                try:
                    payload = json.loads(payload_text)
                except json.JSONDecodeError:
                    payload = payload_text
                events.append({"event": name, "data": payload})
            name = "message"
            data_lines = []
    if data_lines:
        payload_text = "\n".join(data_lines)
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            payload = payload_text
        events.append({"event": name, "data": payload})
    return events


def _slug(index: int, query: str) -> str:
    words = re.findall(r"[a-z0-9]+", query.lower())
    return f"{index:02d}-" + "-".join(words[:8])


def _summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    final = next(
        (item["data"] for item in reversed(events) if item["event"] == "final"), None
    )
    errors = [item["data"] for item in events if item["event"] == "error"]
    final = final if isinstance(final, dict) else {}
    charts = final.get("charts") or []
    return {
        "event_order": [item["event"] for item in events],
        "event_count": len(events),
        "errors": errors,
        "ok": final.get("ok"),
        "seconds": final.get("seconds"),
        "answer": final.get("answer"),
        "final_text": final.get("final_text"),
        "chart_count": len(charts),
        "charts": charts,
        "verifier_status": final.get("verifier_status"),
        "verifier_mode": final.get("verifier_mode"),
        "verifier_changed_answer": final.get("verifier_changed_answer"),
        "verifier_mismatch": final.get("verifier_mismatch"),
        "verifier_audit_text": final.get("verifier_audit_text"),
        "synthesis_draft": final.get("synthesis_draft"),
        "usage": final.get("usage"),
        "steps": final.get("steps"),
        "solver_steps": final.get("solver_steps"),
        "total_calls": final.get("total_calls"),
        "stall_count": final.get("stall_count"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args(argv)

    queries = load_queries(args.queries)
    arms = ["single", "single", "single", "single", "multi"]
    args.out.mkdir(parents=True, exist_ok=True)

    health_before = httpx.get(f"{args.base_url}/api/health", timeout=30).json()
    index = httpx.get(f"{args.base_url}/", timeout=30)
    (args.out / "health-before.json").write_text(
        json.dumps(health_before, indent=2) + "\n", encoding="utf-8"
    )
    (args.out / "index.html").write_text(index.text, encoding="utf-8")
    print(f"health before: {health_before}")
    print(
        f"index: HTTP {index.status_code}, bytes {len(index.text)}, "
        f"ai-notice={'Peak-to-mean ratios are screening signals' in index.text}"
    )

    manifest: list[dict[str, Any]] = []
    with httpx.Client(timeout=args.timeout) as client:
        for position, (query, arm) in enumerate(zip(queries, arms), start=1):
            slug = _slug(position, query)
            started = time.perf_counter()
            print(f"\n[{position}/5] arm={arm} {query}")
            raw_parts: list[str] = []
            with client.stream(
                "POST",
                f"{args.base_url}/api/chat",
                json={"query": query, "arm": arm},
            ) as response:
                if response.status_code != 200:
                    body = response.read().decode("utf-8", "replace")
                    print(f"  HTTP {response.status_code}: {body[:800]}")
                    manifest.append(
                        {
                            "index": position,
                            "slug": slug,
                            "query": query,
                            "arm": arm,
                            "http_status": response.status_code,
                            "error_body": body[:4000],
                        }
                    )
                    continue
                for chunk in response.iter_text():
                    raw_parts.append(chunk)
                    print(chunk, end="")
            raw = "".join(raw_parts)
            events = parse_sse(raw)
            summary = _summary(events)
            elapsed = time.perf_counter() - started
            (args.out / f"{slug}.sse.txt").write_text(raw, encoding="utf-8")
            (args.out / f"{slug}.json").write_text(
                json.dumps(
                    {
                        "index": position,
                        "query": query,
                        "arm": arm,
                        "elapsed_seconds": round(elapsed, 3),
                        "events": events,
                        "summary": summary,
                    },
                    indent=2,
                    ensure_ascii=False,
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )
            manifest.append(
                {
                    "index": position,
                    "slug": slug,
                    "query": query,
                    "arm": arm,
                    "http_status": 200,
                    "elapsed_seconds": round(elapsed, 3),
                    "summary": summary,
                }
            )
            print(
                f"\n  events: {' -> '.join(summary['event_order'])}\n"
                f"  ok={summary['ok']} answer={summary['answer']!r} "
                f"charts={summary['chart_count']} "
                f"verifier={summary['verifier_status']!r} "
                f"errors={len(summary['errors'])}"
            )

    health_after = httpx.get(f"{args.base_url}/api/health", timeout=30).json()
    (args.out / "health-after.json").write_text(
        json.dumps(health_after, indent=2) + "\n", encoding="utf-8"
    )
    (args.out / "manifest.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(timespec="seconds"),
                "base_url": args.base_url,
                "queries_file": str(args.queries),
                "health_before": health_before,
                "health_after": health_after,
                "runs": manifest,
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nhealth after: {health_after}")
    print(f"ARTIFACT: {args.out / 'manifest.json'}")
    return 0 if all(item.get("http_status") == 200 for item in manifest) else 1


if __name__ == "__main__":
    raise SystemExit(main())
