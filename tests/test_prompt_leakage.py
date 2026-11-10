"""Prompt-leakage guard for the baseline sets.

The v1 baseline was contaminated: three of its questions were also worked
examples in the prompt, so a high score measured memorisation. This test fails
if any baseline question or truth SQL shares a 12-token run with the assembled
prompt.

Normalisation:

* lower-case, collapse all non-alphanumeric characters to spaces;
* strip quoted string literals (this removes station names from SQL) and numeric
  literals, as the review requested;
* strip ``wx_<station_code>`` tokens, which are station names outside quotes;
* questions are compared without stripping station names, so a copied question
  cannot hide behind a different station.

The prompt fixture uses the live admin engine when it is reachable so the test
checks the real assembled prompt, including the data-window inventory. On a
clean checkout without Docker it falls back to the static prompt; the examples,
rules and schema are present in both cases, and that is where leakage lives.
"""

from __future__ import annotations

import datetime
import re

import pytest

from core.prompts import HKT
from domains.hk.roles import COORDINATOR_SYSTEM, MULTI_RULES, ROLE_PROMPTS
from domains.hk.schema_prompt import get_system_prompt
from evals import hk_baseline, hk_baseline_v2

WINDOW = 12


def _tokens(text: str) -> list[str]:
    text = text.lower()
    text = re.sub(r"'(?:''|[^'])*'", " ", text)  # SQL string literals
    text = re.sub(r"\b\d+(?:\.\d+)?\b", " ", text)  # numeric literals
    text = re.sub(r"\bwx_[a-z0-9_]+\b", " ", text)  # weather station codes
    text = re.sub(r"[^a-z0-9_]+", " ", text)
    return text.split()


def _shingles(text: str, size: int = WINDOW) -> set[str]:
    tokens = _tokens(text)
    return {" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)}


@pytest.fixture(scope="module")
def prompt_shingles() -> set[str]:
    now = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=HKT)
    engine = None
    try:
        # Prefer the real assembled prompt, including the live data window. A
        # clean checkout without Docker falls back to the static part; the
        # examples and rules, which are where leakage lives, are always present.
        from core.env import admin_url
        from domains.hk.db import make_admin_engine

        engine = make_admin_engine(admin_url())
        prompt = get_system_prompt(engine=engine, now=now)
    except Exception:  # noqa: BLE001
        prompt = get_system_prompt(engine=None, now=now)
    finally:
        if engine is not None:
            engine.dispose()
    multi = "\n\n".join(
        [
            COORDINATOR_SYSTEM,
            MULTI_RULES,
            *ROLE_PROMPTS.values(),
        ]
    )
    return _shingles(prompt + "\n\n" + multi)


@pytest.mark.parametrize("case", hk_baseline.CASES, ids=lambda c: f"v1-{c.id}")
def test_v1_does_not_leak_into_prompt(prompt_shingles: set[str], case):
    # v1 stays in the repo as a contaminated smoke set, but it must not become
    # contaminated again by a future prompt edit.
    assert not (_shingles(case.question) & prompt_shingles), case.question
    assert not (_shingles(case.truth_sql) & prompt_shingles), case.truth_sql


@pytest.mark.parametrize("case", hk_baseline_v2.CASES, ids=lambda c: f"v2-{c.id}")
def test_v2_does_not_leak_into_prompt(prompt_shingles: set[str], case):
    assert not (_shingles(case.question) & prompt_shingles), case.question
    assert not (_shingles(case.truth_sql) & prompt_shingles), case.truth_sql


def test_v2_has_the_required_shape():
    cases = hk_baseline_v2.CASES
    assert len(cases) == 20
    assert {case.category for case in cases} == {
        "retrieval",
        "temporal",
        "spatial",
        "statistics",
        "multi_table",
    }
    for category in {case.category for case in cases}:
        assert sum(case.category == category for case in cases) == 4

    assert sum(case.holdout for case in cases) == 6
    assert sum(case.kind == "unanswerable" for case in cases) == 2
    assert sum(case.kind == "text" for case in cases) == 3  # two NULL traps + one station name
    assert all(case.difficulty in {"simple", "medium", "hard"} for case in cases)

    # The four fuzzy names named in the review must appear in the set.
    questions = " ".join(case.question.lower() for case in cases)
    for name in ("shatin", "tung chung", "causeway bay", "central/western"):
        assert name in questions


def test_v2_is_compositional():
    """At least ten cases must touch two tables or use a spatial join."""
    compositional = 0
    for case in hk_baseline_v2.CASES:
        sql = case.truth_sql.lower()
        if (
            " join " in f" {sql} "
            or "st_dwithin" in sql
            or "st_distance" in sql
            or re.search(r"from\s+\w+\s+\w+\s*,", sql)
        ):
            compositional += 1
    assert compositional >= 10, compositional
