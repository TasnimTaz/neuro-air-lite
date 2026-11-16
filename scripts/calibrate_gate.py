"""Calibrate the relevance-gate threshold on a held-out split.

    uv run python scripts/calibrate_gate.py
    uv run python scripts/calibrate_gate.py --data evals/gate_set.json --seed 0

This script makes live embedding requests to the local Ollama endpoint. Do not
run it while a baseline or verifier-mode evaluation is running: the embedding
model can evict the chat model from memory and change the evaluation's latency.
It is also why Day 6 only writes and unit-tests the script; the real calibration
runs after the background evaluations and after:

    ollama pull nomic-embed-text

The reported number is the accuracy on the held-out half. The train half is used
only to choose theta; it is printed for transparency, not as the result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.embeddings import EmbeddingClient  # noqa: E402
from core.gate import RelevanceGate  # noqa: E402

DEFAULT_DATA = ROOT / "evals" / "gate_set.json"
THETAS = [round(0.30 + 0.02 * index, 2) for index in range(21)]  # 0.30..0.70


@dataclass(frozen=True)
class GateItem:
    id: str
    query: str
    label: int
    group: str


@dataclass(frozen=True)
class ScoredItem:
    item: GateItem
    score: float


def load_items(path: str | Path = DEFAULT_DATA) -> list[GateItem]:
    """Load and validate the gate calibration JSON."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("gate set must be a JSON object")
    positive_groups = payload.get("positive_groups") or ["in_scope", "vague_in_scope"]
    items: list[GateItem] = []
    for group in ("in_scope", "out_of_scope", "vague_in_scope"):
        entries = payload.get(group)
        if not isinstance(entries, list):
            raise ValueError(f"gate set is missing the {group!r} list")
        label = 1 if group in positive_groups else 0
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"{group} entries must be objects")
            item_id = str(entry.get("id", "")).strip()
            query = str(entry.get("query", "")).strip()
            if not item_id or not query:
                raise ValueError(f"{group} entry needs a non-empty id and query")
            items.append(GateItem(id=item_id, query=query, label=label, group=group))
    return items


def split_items(
    items: Sequence[GateItem], *, seed: int = 0
) -> tuple[list[GateItem], list[GateItem]]:
    """Stratified 50/50 split by group, deterministic for a given seed."""
    rng = Random(seed)
    by_group: dict[str, list[GateItem]] = {}
    for item in items:
        by_group.setdefault(item.group, []).append(item)

    train: list[GateItem] = []
    test: list[GateItem] = []
    for group in sorted(by_group):
        group_items = list(by_group[group])
        rng.shuffle(group_items)
        cut = len(group_items) // 2
        test.extend(group_items[:cut])
        train.extend(group_items[cut:])
    return train, test


def accuracy(items: Iterable[ScoredItem], theta: float) -> float:
    """Fraction of items whose score is on the correct side of ``theta``."""
    evaluated = list(items)
    if not evaluated:
        return 0.0
    correct = sum((item.score >= theta) == (item.item.label == 1) for item in evaluated)
    return correct / len(evaluated)


def select_theta(train: Sequence[ScoredItem], candidates: Sequence[float]) -> tuple[float, float]:
    """Choose theta on train only; ties go to the value closest to 0.50."""
    scores = {theta: accuracy(train, theta) for theta in candidates}
    best = max(candidates, key=lambda theta: (scores[theta], -abs(theta - 0.50)))
    return best, scores[best]


def confusion(items: Sequence[ScoredItem], theta: float) -> dict[str, int]:
    """Count false positives/negatives and true positives/negatives."""
    counts = {"tp": 0, "tn": 0, "fp": 0, "fn": 0}
    for item in items:
        predicted = item.score >= theta
        actual = item.item.label == 1
        if predicted and actual:
            counts["tp"] += 1
        elif predicted and not actual:
            counts["fp"] += 1
        elif not predicted and actual:
            counts["fn"] += 1
        else:
            counts["tn"] += 1
    return counts


async def calibrate(
    items: Sequence[GateItem], *, seed: int = 0
) -> tuple[list[ScoredItem], list[ScoredItem], dict[str, object]]:
    """Embed every query once, split, and select theta on the train half."""
    client = EmbeddingClient()
    gate = RelevanceGate(embedder=client.embed)
    scores = await gate.score_many([item.query for item in items])
    scored = [ScoredItem(item=item, score=score) for item, score in zip(items, scores)]
    by_id = {item.item.id: item for item in scored}

    train_ids, test_ids = split_items(items, seed=seed)
    train = [by_id[item.id] for item in train_ids]
    test = [by_id[item.id] for item in test_ids]

    selected, train_accuracy = select_theta(train, THETAS)
    test_accuracy = accuracy(test, selected)
    report = {
        "model": client.model,
        "seed": seed,
        "theta": selected,
        "train_accuracy": round(train_accuracy, 4),
        "heldout_accuracy": round(test_accuracy, 4),
        "train_counts": confusion(train, selected),
        "heldout_counts": confusion(test, selected),
        "train_n": len(train),
        "heldout_n": len(test),
    }
    return train, test, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    print("WARNING: this script makes live embedding calls. Do not run it while an eval is running.")
    items = load_items(args.data)
    train, test, report = asyncio.run(calibrate(items, seed=args.seed))

    print(f"{'theta':>6} {'train':>8} {'held-out':>9}")
    for theta in THETAS:
        print(f"{theta:>6.2f} {accuracy(train, theta):>8.3f} {accuracy(test, theta):>9.3f}")
    print()
    print(
        "Reported held-out accuracy: "
        f"{report['heldout_accuracy']:.3f} at theta={report['theta']:.2f} "
        f"(selected on {report['train_n']} disjoint train items; "
        f"{report['heldout_n']} held-out items)"
    )
    print(f"train counts (selection only): {report['train_counts']}")
    print(f"held-out counts: {report['heldout_counts']}")
    print(f"embedding model: {report['model']}")

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
