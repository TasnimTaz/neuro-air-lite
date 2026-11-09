"""LLM client for Neuro-Air Lite using AsyncOpenAI.

Connects to an OpenAI-compatible endpoint (Ollama by default, or cloud API).
Tracks prompt/completion token usage and per-call latency in module-level USAGE.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
import openai
from openai import AsyncOpenAI

USAGE: dict[str, Any] = {
    "prompt": 0,
    "completion": 0,
    "calls": 0,
    "total_seconds": 0.0,
}

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        base_url = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:11434/v1")
        api_key = os.environ.get("LLM_API_KEY", "ollama")
        _client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=180.0,
        )
    return _client


def snapshot_usage() -> dict[str, Any]:
    """Copy the current counters so a caller can compute a delta around a run."""
    return dict(USAGE)


def reset_usage() -> dict[str, Any]:
    """Reset the module-level USAGE accumulator and return previous counts."""
    prev = dict(USAGE)
    USAGE["prompt"] = 0
    USAGE["completion"] = 0
    USAGE["calls"] = 0
    USAGE["total_seconds"] = 0.0
    return prev


async def llm(
    messages: list[dict[str, str]],
    model: str | None = None,
    temperature: float = 0.2,
) -> str:
    """Send a chat completion request with token and latency tracking.

    Retries once on network/transport errors.
    """
    client = get_client()
    target_model = model or os.environ.get("LLM_MODEL", "qwen2.5-coder:14b")

    retries = 1
    last_err: Exception | None = None

    for attempt in range(retries + 1):
        started = time.perf_counter()
        try:
            resp = await client.chat.completions.create(
                model=target_model,
                messages=messages,  # type: ignore[arg-type]
                temperature=temperature,
            )
            elapsed = time.perf_counter() - started

            content = resp.choices[0].message.content or ""
            prompt_tokens = resp.usage.prompt_tokens if resp.usage else 0
            completion_tokens = resp.usage.completion_tokens if resp.usage else 0

            USAGE["prompt"] += prompt_tokens
            USAGE["completion"] += completion_tokens
            USAGE["calls"] += 1
            USAGE["total_seconds"] += elapsed

            return content
        except (openai.APIConnectionError, openai.APITimeoutError, httpx.TransportError) as exc:
            last_err = exc
            if attempt < retries:
                await asyncio.sleep(1.0)
                continue
            raise

    raise RuntimeError(f"llm call failed after retries: {last_err}") from last_err
