"""Minimal end-to-end LLM smoke test.

Sends one prompt that should come back as a single fenced python block, which is
the contract every worker relies on from Day 3 onward.

    uv run python scripts/llm_smoke.py
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

from openai import OpenAI  # noqa: E402 - needs .env loaded first

FENCE = "`" * 3
CODE_RE = re.compile(FENCE + r"python\s*(.*?)" + FENCE, re.S)

PROMPT = (
    "You are a Python data analyst. Reply with exactly one ```python code block "
    "that computes the mean of [1, 2, 3, 4] and prints it. No text after the block."
)


def main() -> int:
    model = os.environ["LLM_MODEL"]
    client = OpenAI(
        base_url=os.environ["LLM_BASE_URL"],
        api_key=os.environ["LLM_API_KEY"],
        timeout=180.0,
    )

    print(f"model: {model}")
    started = time.perf_counter()
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": PROMPT}],
        temperature=0.2,
    )
    elapsed = time.perf_counter() - started

    text = resp.choices[0].message.content or ""
    usage = resp.usage
    match = CODE_RE.search(text)
    if not match:
        print("[FAIL] no fenced python block in the reply")
        print(text[:600])
        return 1

    code = match.group(1).strip()
    namespace: dict[str, object] = {}
    try:
        exec(compile(code, "<smoke>", "exec"), namespace)  # noqa: S102 - fixture
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] returned code does not run: {exc}")
        print(code)
        return 1

    gen = getattr(usage, "completion_tokens", None) if usage else None
    rate = f"{gen / elapsed:.1f} tok/s" if gen and elapsed else "n/a"
    print(f"[ ok ] code block found and executed in {elapsed:.1f}s ({rate})")
    print("----- returned code -----")
    print(code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
