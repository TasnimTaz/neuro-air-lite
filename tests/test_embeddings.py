"""Embedding client tests: batching, cache keys and offline fakes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.embeddings import EmbeddingClient, cosine


def _vector(text: str) -> list[float]:
    return [float(len(text)), float(sum(ord(char) for char in text) % 13), 1.0]


class FakeEmbeddings:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    async def create(self, *, model: str, input: list[str]):
        self.calls.append((model, list(input)))
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=index, embedding=_vector(text))
                for index, text in enumerate(input)
            ]
        )


class FakeClient:
    def __init__(self) -> None:
        self.embeddings = FakeEmbeddings()


async def test_embed_caches_by_model_and_text(tmp_path):
    fake = FakeClient()
    client = EmbeddingClient(model="test-model", cache_dir=tmp_path, client=fake)

    first = await client.embed(["alpha", "beta"])
    second = await client.embed(["alpha", "beta"])

    assert fake.embeddings.calls == [("test-model", ["alpha", "beta"])]
    assert first == second
    assert client.cache_path("alpha").is_file()
    assert client.cache_path("beta").is_file()


async def test_cache_hit_survives_a_new_client_instance(tmp_path):
    first_fake = FakeClient()
    first = EmbeddingClient(model="test-model", cache_dir=tmp_path, client=first_fake)
    await first.embed(["alpha"])

    second_fake = FakeClient()
    second = EmbeddingClient(model="test-model", cache_dir=tmp_path, client=second_fake)
    assert await second.embed(["alpha"]) == await first.embed(["alpha"])
    assert second_fake.embeddings.calls == []


async def test_different_model_uses_a_different_cache_key(tmp_path):
    first_fake = FakeClient()
    first = EmbeddingClient(model="model-a", cache_dir=tmp_path, client=first_fake)
    await first.embed(["alpha"])

    second_fake = FakeClient()
    second = EmbeddingClient(model="model-b", cache_dir=tmp_path, client=second_fake)
    await second.embed(["alpha"])

    assert first.cache_path("alpha") != second.cache_path("alpha")
    assert len(second_fake.embeddings.calls) == 1


async def test_duplicate_texts_are_embedded_once(tmp_path):
    fake = FakeClient()
    client = EmbeddingClient(model="test-model", cache_dir=tmp_path, client=fake)

    vectors = await client.embed(["same", "same"])

    assert fake.embeddings.calls == [("test-model", ["same"])]
    assert vectors[0] == vectors[1]


async def test_corrupt_cache_file_is_refetched(tmp_path):
    fake = FakeClient()
    client = EmbeddingClient(model="test-model", cache_dir=tmp_path, client=fake)
    await client.embed(["alpha"])

    client.cache_path("alpha").write_text("{not json", encoding="utf-8")

    second_fake = FakeClient()
    second = EmbeddingClient(model="test-model", cache_dir=tmp_path, client=second_fake)
    assert await second.embed(["alpha"]) == await client.embed(["alpha"])
    assert len(second_fake.embeddings.calls) == 1


async def test_empty_input_makes_no_request(tmp_path):
    fake = FakeClient()
    client = EmbeddingClient(model="test-model", cache_dir=tmp_path, client=fake)

    assert await client.embed([]) == []
    assert fake.embeddings.calls == []


def test_model_comes_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("EMBED_MODEL", "env-embed-model")
    client = EmbeddingClient(cache_dir=tmp_path, client=FakeClient())
    assert client.model == "env-embed-model"


def test_prefixes_default_to_nomic_and_honour_empty_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("EMBED_QUERY_PREFIX", raising=False)
    monkeypatch.delenv("EMBED_DOC_PREFIX", raising=False)
    default = EmbeddingClient(cache_dir=tmp_path, client=FakeClient())
    assert default.query_prefix == "search_query: "
    assert default.doc_prefix == "search_document: "

    monkeypatch.setenv("EMBED_QUERY_PREFIX", "")
    monkeypatch.setenv("EMBED_DOC_PREFIX", "")
    empty = EmbeddingClient(cache_dir=tmp_path, client=FakeClient())
    assert empty.query_prefix == ""
    assert empty.doc_prefix == ""


async def test_query_and_document_prefixes_are_separate_cache_entries(tmp_path):
    fake = FakeClient()
    client = EmbeddingClient(
        model="test-model",
        cache_dir=tmp_path,
        query_prefix="Q: ",
        doc_prefix="DOC: ",
        client=fake,
    )

    query_vector = await client.embed_query(["alpha"])
    document_vector = await client.embed_documents(["alpha"])

    assert fake.embeddings.calls == [
        ("test-model", ["Q: alpha"]),
        ("test-model", ["DOC: alpha"]),
    ]
    assert query_vector != document_vector
    assert client.cache_path("alpha", prefix="Q: ") != client.cache_path(
        "alpha", prefix="DOC: "
    )
    assert client.cache_path("alpha", prefix="Q: ").is_file()

    assert await client.embed_query(["alpha"]) == query_vector
    assert await client.embed_documents(["alpha"]) == document_vector
    assert len(fake.embeddings.calls) == 2


def test_cosine_handles_orthogonal_and_zero_vectors():
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([0.0, 0.0], [1.0, 1.0]) == pytest.approx(0.0)
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_rejects_mismatched_dimensions():
    with pytest.raises(ValueError, match="length mismatch"):
        cosine([1.0], [1.0, 2.0])
