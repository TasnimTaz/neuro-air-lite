import math

import numpy as np
import pytest

from core.verifier_safe import UNSET, Decision, is_sentinel_text, kind_of, resolve


@pytest.mark.parametrize(
    "pre,post,accept",
    [
        (4.24, 3.71, True),            # valid numeric rewrite is accepted (known residual risk)
        (4.24, math.nan, False),
        (4.24, math.inf, False),
        (4.24, "No data available", False),
        (4.24, [1, 2], False),
        (UNSET, 12.0, True),           # verifier fills a missing value
        (UNSET, math.nan, False),
        (4.24, UNSET, True),           # verifier says unanswerable
        ("Tai Po", "Sha Tin", True),
        (np.int64(3), np.float64(3.0), True),
        (3.0, 3.0, True),
        (math.nan, 5.0, True),         # staged NaN repaired
    ],
)
def test_resolve(pre, post, accept):
    assert resolve(pre, post).accept is accept


def test_kind_of_unwraps_numpy():
    assert kind_of(np.float64(1.0)) == "number"
    assert kind_of(np.float64("nan")) == "nonfinite"
    assert kind_of(UNSET) == "unset"
    assert kind_of(True) == "other"


def test_sentinels():
    assert is_sentinel_text("[STEP_BUDGET_EXHAUSTED]")
    assert is_sentinel_text("I stopped because the identical code block was about to run again. Use ...")
    assert is_sentinel_text("   ")
    assert not is_sentinel_text("VERIFY x: stored=1 | independent=1 | final=1")
