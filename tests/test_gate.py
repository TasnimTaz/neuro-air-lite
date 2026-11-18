"""Relevance-gate tests with a deterministic fake embedder (no network)."""

from __future__ import annotations

import pytest

from core.gate import DEFAULT_THETA, MAX_QUERY_CHARS, SCOPE, RelevanceGate


def make_embedder(vectors: dict[str, list[float]], default: list[float] | None = None):
    calls: list[list[str]] = []
    fallback = default or [0.0, 0.0, 1.0]

    async def fake(texts):
        calls.append(list(texts))
        return [vectors.get(text.lower(), fallback) for text in texts]

    return fake, calls


async def test_gate_picks_nearest_scope_sentence():
    scope = ["PM2.5 air quality", "wind speed weather"]
    fake, _ = make_embedder(
        {
            "pm2.5 air quality": [1.0, 0.0, 0.0],
            "wind speed weather": [0.0, 1.0, 0.0],
            "pm2.5": [1.0, 0.0, 0.0],
            "wind": [0.0, 1.0, 0.0],
            "pizza": [0.0, 0.0, 1.0],
        }
    )
    gate = RelevanceGate(scope=scope, theta=0.50, embedder=fake)

    decision = await gate.decide("PM2.5")

    assert decision["in_scope"] is True
    assert decision["score"] == pytest.approx(1.0)
    assert decision["nearest_scope"] == "PM2.5 air quality"
    assert decision["reason"] == "ok"

    pizza = await gate.decide("pizza")
    assert pizza["in_scope"] is False
    assert pizza["score"] == pytest.approx(0.0)
    assert pizza["nearest_scope"] == "PM2.5 air quality"


async def test_threshold_boundary_is_inclusive():
    fake, _ = make_embedder(
        {
            "only axis": [1.0, 0.0],
            "boundary": [0.8, 0.6],
        }
    )

    at_boundary = await RelevanceGate(scope=["only axis"], theta=0.80, embedder=fake).decide(
        "boundary"
    )
    above_boundary = await RelevanceGate(
        scope=["only axis"], theta=0.81, embedder=fake
    ).decide("boundary")

    assert at_boundary["score"] == pytest.approx(0.8)
    assert at_boundary["in_scope"] is True
    assert above_boundary["in_scope"] is False


async def test_empty_and_overlong_queries_never_embed():
    fake, calls = make_embedder({"x": [1.0, 0.0]})
    gate = RelevanceGate(scope=["scope"], theta=0.5, embedder=fake)

    empty = await gate.decide("   ")
    long = await gate.decide("x" * (MAX_QUERY_CHARS + 1))

    assert empty["reason"] == "empty_query"
    assert long["reason"] == "query_too_long"
    assert empty["in_scope"] is False
    assert long["in_scope"] is False
    assert calls == []


async def test_score_many_reuses_scope_embeddings_and_rejects_invalid():
    fake, calls = make_embedder(
        {
            "scope": [1.0, 0.0],
            "one": [1.0, 0.0],
            "two": [0.0, 1.0],
        }
    )
    gate = RelevanceGate(scope=["scope"], theta=0.5, embedder=fake)

    scores = await gate.score_many(["one", "", "two", "x" * (MAX_QUERY_CHARS + 1)])

    assert scores[0] == pytest.approx(1.0)
    assert scores[1] == 0.0
    assert scores[2] == pytest.approx(0.0)
    assert scores[3] == 0.0
    # Scope + the two valid queries, not one call per query.
    assert calls[0] == ["scope"]
    assert calls[1] == ["one", "two"]


async def test_gate_uses_separate_document_and_query_embedders():
    document_calls: list[list[str]] = []
    query_calls: list[list[str]] = []

    async def documents(texts):
        document_calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    async def queries(texts):
        query_calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    gate = RelevanceGate(
        scope=["scope document"],
        theta=0.5,
        embedder=queries,
        doc_embedder=documents,
    )

    decision = await gate.decide("scope query")

    assert decision["in_scope"] is True
    assert document_calls == [["scope document"]]
    assert query_calls == [["scope query"]]


def test_theta_comes_from_environment(monkeypatch):
    monkeypatch.setenv("GATE_THETA", "0.63")
    gate = RelevanceGate(scope=["s"], embedder=lambda texts: None)  # type: ignore[arg-type]
    assert gate.theta == pytest.approx(0.63)


def test_invalid_theta_is_rejected(monkeypatch):
    monkeypatch.setenv("GATE_THETA", "not-a-number")
    with pytest.raises(ValueError, match="GATE_THETA"):
        RelevanceGate(scope=["s"], embedder=lambda texts: None)  # type: ignore[arg-type]


def test_default_scope_is_the_documented_size():
    assert 15 <= len(SCOPE) <= 20
    assert DEFAULT_THETA == pytest.approx(0.54)
