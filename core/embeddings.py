"""Embedding client for the local Ollama OpenAI-compatible endpoint.

The project deliberately does not use ``sentence-transformers`` or PyTorch for
embeddings. Ollama already serves an OpenAI-compatible ``/v1/embeddings`` route,
so the same local process that serves the chat model can serve embeddings:

    ollama pull nomic-embed-text
    # EMBED_MODEL defaults to nomic-embed-text; EMBED_BASE_URL falls back to
    # LLM_BASE_URL, then http://127.0.0.1:11434/v1.

Embeddings are cached on disk by ``(model, sha256(text))``. The cache lives
under ``ingest/cache/embeddings`` by default (gitignored), and the location can
be overridden with ``EMBED_CACHE_DIR``. A cache hit makes no network request,
which matters for the relevance gate and the knowledge base because their scope
text does not change between runs.

The embedder is injectable: tests pass a deterministic fake client instead of
contacting Ollama. The public ``embed`` function is async; a caller that needs a
synchronous result must drive it on an event loop (the knowledge-base wrapper in
``domains.hk.kb`` does that for the runtime tool).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "nomic-embed-text"
DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_CACHE_DIR = ROOT / "ingest" / "cache" / "embeddings"
DEFAULT_BATCH_SIZE = 64

# A fake embedder implements this shape: texts -> one vector per text.
Embedder = Callable[[Sequence[str]], Awaitable[Sequence[Sequence[float]]]]


class EmbeddingError(RuntimeError):
    """Raised when the embedding endpoint returns an unusable response."""


def model_name(model: str | None = None) -> str:
    """Return the configured embedding model."""
    return model or os.environ.get("EMBED_MODEL", DEFAULT_MODEL)


def cache_dir_path(path: str | Path | None = None) -> Path:
    """Return the on-disk embedding cache directory."""
    if path is not None:
        return Path(path).expanduser()
    return Path(os.environ.get("EMBED_CACHE_DIR", str(DEFAULT_CACHE_DIR))).expanduser()


def text_sha256(text: str) -> str:
    """Return the cache key component for one input text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _model_slug(model: str) -> str:
    """A filesystem-safe directory name for a model identifier."""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", model).strip("._-")
    return slug or "model"


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity, returning 0.0 for a zero vector.

    Embedding endpoints normally return finite values, but a zero vector is
    possible for an empty or degenerate input. Returning 0.0 keeps the gate
    deterministic instead of raising a division-by-zero error.
    """
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} != {len(b)}")
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for left, right in zip(a, b):
        x = float(left)
        y = float(right)
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("embedding contains a non-finite value")
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    value = dot / math.sqrt(norm_a * norm_b)
    # Floating-point round-off can put this just outside [-1, 1].
    return max(-1.0, min(1.0, value))


class EmbeddingClient:
    """Async OpenAI-compatible embedding client with an on-disk cache.

    Args:
        model: Embedding model name. Defaults to ``EMBED_MODEL`` or
            ``nomic-embed-text``.
        base_url: OpenAI-compatible base URL. Defaults to ``EMBED_BASE_URL``,
            then ``LLM_BASE_URL``, then local Ollama.
        api_key: API key. Defaults to ``EMBED_API_KEY``, then ``LLM_API_KEY``,
            then ``ollama``.
        cache_dir: Cache root. Defaults to ``EMBED_CACHE_DIR`` or
            ``ingest/cache/embeddings``.
        client: Optional already-constructed client-like object exposing
            ``embeddings.create``. Tests use this to avoid the network.
        use_cache: Set false to bypass reads and writes (diagnostics only).
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        cache_dir: str | Path | None = None,
        client: Any | None = None,
        timeout: float = 60.0,
        use_cache: bool = True,
    ) -> None:
        self.model = model_name(model)
        self.cache_root = cache_dir_path(cache_dir)
        self.use_cache = use_cache
        self._client = client
        if self._client is None:
            resolved_base = (
                base_url
                or os.environ.get("EMBED_BASE_URL")
                or os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL)
            )
            resolved_key = (
                api_key
                or os.environ.get("EMBED_API_KEY")
                or os.environ.get("LLM_API_KEY", "ollama")
            )
            self._client = AsyncOpenAI(
                base_url=resolved_base,
                api_key=resolved_key,
                timeout=timeout,
            )

    def cache_path(self, text: str) -> Path:
        """Return the cache file for one ``(model, text)`` pair."""
        return self.cache_root / _model_slug(self.model) / f"{text_sha256(text)}.json"

    def _read_cache(self, text: str) -> list[float] | None:
        path = self.cache_path(text)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("model") != self.model:
            return None
        if payload.get("text_sha256") != text_sha256(text):
            return None
        vector = payload.get("embedding")
        if not isinstance(vector, list) or not vector:
            return None
        try:
            return [float(value) for value in vector]
        except (TypeError, ValueError):
            return None

    def _write_cache(self, text: str, vector: Sequence[float]) -> None:
        if not self.use_cache:
            return
        path = self.cache_path(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": self.model,
            "text_sha256": text_sha256(text),
            "embedding": [float(value) for value in vector],
        }
        # Atomic replace: a killed process leaves either the old file or the new
        # one, never a half-written JSON document.
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

    async def _request(self, texts: Sequence[str]) -> list[list[float]]:
        response = await self._client.embeddings.create(model=self.model, input=list(texts))
        data = list(getattr(response, "data", []) or [])
        if len(data) != len(texts):
            raise EmbeddingError(
                f"embedding response has {len(data)} vectors for {len(texts)} inputs"
            )

        ordered: list[list[float] | None] = [None] * len(texts)
        for position, item in enumerate(data):
            index = getattr(item, "index", position)
            if not isinstance(index, int) or not 0 <= index < len(texts):
                raise EmbeddingError(f"invalid embedding index: {index!r}")
            if ordered[index] is not None:
                raise EmbeddingError(f"duplicate embedding index: {index}")
            raw = getattr(item, "embedding", None)
            if not isinstance(raw, (list, tuple)) or not raw:
                raise EmbeddingError(f"empty embedding at index {index}")
            vector = []
            for value in raw:
                number = float(value)
                if not math.isfinite(number):
                    raise EmbeddingError(f"non-finite embedding value at index {index}")
                vector.append(number)
            ordered[index] = vector

        if any(vector is None for vector in ordered):
            raise EmbeddingError("embedding response did not cover every input")
        return [vector for vector in ordered if vector is not None]

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed texts, using the cache for texts already seen with this model."""
        items = [str(text) for text in texts]
        if not items:
            return []

        keys = [text_sha256(text) for text in items]
        by_key: dict[str, list[float]] = {}
        missing: dict[str, str] = {}
        for text, key in zip(items, keys):
            if key in by_key or key in missing:
                continue
            cached = self._read_cache(text) if self.use_cache else None
            if cached is not None:
                by_key[key] = cached
            else:
                missing[key] = text

        pending = list(missing.items())
        for start in range(0, len(pending), DEFAULT_BATCH_SIZE):
            batch = pending[start : start + DEFAULT_BATCH_SIZE]
            vectors = await self._request([text for _, text in batch])
            for (key, text), vector in zip(batch, vectors):
                by_key[key] = vector
                self._write_cache(text, vector)

        return [list(by_key[key]) for key in keys]


_default_client: EmbeddingClient | None = None


def get_default_client() -> EmbeddingClient:
    """Return the process-wide lazy embedding client."""
    global _default_client
    if _default_client is None:
        _default_client = EmbeddingClient()
    return _default_client


async def embed(texts: Sequence[str]) -> list[list[float]]:
    """Embed texts with the default local Ollama client and on-disk cache."""
    return await get_default_client().embed(texts)
