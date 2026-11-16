"""Relevance gate for the Hong Kong air-quality domain.

The gate is a deliberately simple retrieval check:

    R(q, D) = max cosine(embed(q), embed(sentence)) over the scope sentences D

If the best cosine is at least ``GATE_THETA`` the query is in scope. This is a
capability gate, not a fact checker: it says whether the question belongs to the
six populated tables (hourly pollutants/AQHI, hourly weather, station metadata,
OSM features, sensitive areas and Hong Kong spatial distance), not whether the
question's premise is true. Premise traps are a separate set.

``GATE_THETA`` overrides the threshold. The built-in default is provisional: it
must be replaced by the held-out result of ``scripts/calibrate_gate.py`` after
the background evaluation runs finish and the embedding model is available.
Until that calibration exists, do not describe the default as tuned.

Empty queries and queries longer than ``MAX_QUERY_CHARS`` are rejected before
any embedding request. The embedder is injectable so tests can use a
deterministic fake and no network.
"""

from __future__ import annotations

import os
from typing import Sequence

from core.embeddings import Embedder, cosine, embed

MAX_QUERY_CHARS = 2000
# Provisional pre-calibration value. Replace from the held-out calibration.
DEFAULT_THETA = 0.50

# One short sentence per capability of the six populated tables. This list is
# data for the gate, not prompt text: it is not injected into the agent prompt
# and it does not contain a v2 question or truth SQL.
SCOPE: tuple[str, ...] = (
    "Hourly air-quality monitoring in Hong Kong: AQHI and PM2.5, PM10, ozone, nitrogen dioxide, sulphur dioxide and carbon monoxide at fixed stations during 2025.",
    "Hourly weather observations at Hong Kong stations: temperature, humidity, pressure, wind speed, wind gust, wind degree and wind direction.",
    "Air-quality station metadata: English name, station code, general or roadside type, urban, rural or new-town area, and coordinates.",
    "Weather station metadata and the one-to-one pairing between a weather station and its air-quality station.",
    "OpenStreetMap features in Hong Kong: schools, hospitals, parks, industrial sites, public facilities and building footprints.",
    "Sensitive areas: educational institutions, health-care institutions and places of worship with point locations.",
    "Spatial questions within Hong Kong: distance in metres, radius searches, nearest station or facility, and points inside a circle.",
    "Time-window questions about Hong Kong air quality or weather in 2025, including daily, monthly and hourly summaries.",
    "Aggregate statistics for Hong Kong monitoring data: mean, maximum, minimum, count, percentiles and thresholds.",
    "Count hours or days when a pollutant, AQHI, temperature or wind value crosses a threshold.",
    "Compare two Hong Kong stations, two pollutants, two time windows or two nearby areas.",
    "Find records for a named Hong Kong district, address, building or landmark after resolving its location.",
    "Join hourly air-quality and hourly weather readings for the same station and hour.",
    "Check data availability, NULL measurements, missing hours and the 2025 coverage window.",
    "Explain Hong Kong air-quality objectives, AQHI bands or WHO guideline values using the local knowledge base.",
    "Summarise or chart a time series of Hong Kong pollutant or weather measurements.",
    "Rank Hong Kong stations or areas by an air-quality or weather metric.",
    "Answer a question about air quality near a Hong Kong school, hospital, park or other sensitive receptor.",
)


def _theta(theta: float | None = None) -> float:
    """Resolve and validate the gate threshold."""
    if theta is None:
        raw = os.environ.get("GATE_THETA")
        if raw is None:
            return DEFAULT_THETA
        try:
            theta = float(raw)
        except ValueError as exc:
            raise ValueError(f"GATE_THETA must be a number, got {raw!r}") from exc
    value = float(theta)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"GATE_THETA must be between 0 and 1, got {value}")
    return value


class RelevanceGate:
    """Cosine gate over a fixed scope-sentence set.

    Args:
        scope: Optional replacement scope (tests use a toy set).
        theta: Optional threshold; otherwise ``GATE_THETA``/``DEFAULT_THETA``.
        embedder: Async callable ``texts -> vectors``. Defaults to
            ``core.embeddings.embed``. Tests inject a fake.
    """

    def __init__(
        self,
        *,
        scope: Sequence[str] | None = None,
        theta: float | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self.scope = tuple(scope or SCOPE)
        self.theta = _theta(theta)
        self._embedder = embedder or embed
        self._scope_vectors: list[list[float]] | None = None

    async def _vectors(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = await self._embedder(list(texts))
        if len(vectors) != len(texts):
            raise ValueError(
                f"embedder returned {len(vectors)} vectors for {len(texts)} texts"
            )
        return [[float(value) for value in vector] for vector in vectors]

    async def _scope_matrix(self) -> list[list[float]]:
        if self._scope_vectors is None:
            if not self.scope:
                raise ValueError("relevance gate needs at least one scope sentence")
            self._scope_vectors = await self._vectors(self.scope)
            width = len(self._scope_vectors[0])
            if any(len(vector) != width for vector in self._scope_vectors):
                raise ValueError("scope embeddings have inconsistent dimensions")
        return self._scope_vectors

    @staticmethod
    def _best_score(
        query_vector: Sequence[float], scope_vectors: Sequence[Sequence[float]]
    ) -> tuple[float, int]:
        best_score = -1.0
        best_index = 0
        for index, vector in enumerate(scope_vectors):
            score = cosine(query_vector, vector)
            if score > best_score:
                best_score = score
                best_index = index
        return best_score, best_index

    async def decide(self, query: str) -> dict[str, object]:
        """Return ``{in_scope, score, nearest_scope}`` for one query.

        Extra fields (``theta`` and ``reason``) are included for logging. The
        empty and overlong checks happen before the embedder is called.
        """
        text = (query or "").strip()
        if not text:
            return {
                "in_scope": False,
                "score": 0.0,
                "nearest_scope": "",
                "theta": self.theta,
                "reason": "empty_query",
            }
        if len(text) > MAX_QUERY_CHARS:
            return {
                "in_scope": False,
                "score": 0.0,
                "nearest_scope": "",
                "theta": self.theta,
                "reason": "query_too_long",
            }

        scope_vectors = await self._scope_matrix()
        query_vector = (await self._vectors([text]))[0]
        score, index = self._best_score(query_vector, scope_vectors)
        return {
            "in_scope": score >= self.theta,
            "score": round(score, 6),
            "nearest_scope": self.scope[index],
            "theta": self.theta,
            "reason": "ok",
        }

    async def score_many(self, queries: Sequence[str]) -> list[float]:
        """Return best cosine scores for a batch; invalid queries score 0.0.

        This is the calibration path: it embeds all valid queries in one batch
        and reuses the cached scope embeddings.
        """
        texts = [(query or "").strip() for query in queries]
        valid_positions = [
            position
            for position, text in enumerate(texts)
            if text and len(text) <= MAX_QUERY_CHARS
        ]
        scores = [0.0] * len(texts)
        if not valid_positions:
            return scores

        scope_vectors = await self._scope_matrix()
        query_vectors = await self._vectors([texts[position] for position in valid_positions])
        for position, vector in zip(valid_positions, query_vectors):
            score, _ = self._best_score(vector, scope_vectors)
            scores[position] = score
        return scores
