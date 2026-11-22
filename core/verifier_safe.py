"""Safe verifier acceptance rules (verifier_mode="safe").

Evidence from the v2/v3/v4 verifier audits (docs/multiagent_failure_log.md):

* the verifier rescued 25-28 wrong answers per run, so it is worth keeping;
* but it also (a) wrote NaN or a string such as "No data available" into a
  numeric ANSWER, (b) replaced a correct refusal with the stall sentinel, and
  (c) overwrote a correct staged value with a wrong recomputation (#18: 4.24 ->
  3.71 m/s).

``overwrite`` trusts every verifier write. ``flag`` trusts none. ``safe`` takes the
middle road and encodes only rules that need no knowledge of the task:

1. A verifier write is rejected if it is not a valid value of the same *kind* as
   the staged one: NaN/inf, a string where the staged value was numeric, or a
   container. The staged value is restored.
2. If the verifier ended on a harness sentinel (stall guard or exhausted budget)
   its text is discarded; the synthesis text is kept.
3. A numeric verifier write that is valid is accepted (it is the whole point of the
   verifier). Case (c) above is NOT fixed by these rules; that needs a second
   independent recomputation or a stronger model, and is intentionally left for
   the flag-vs-overwrite-vs-safe comparison to measure.

``resolve`` is pure so it can be unit-tested without a runtime.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

UNSET = object()

BUDGET_EXHAUSTED_SENTINEL = "[STEP_BUDGET_EXHAUSTED]"
STALL_TEXT_PREFIX = "I stopped because the identical code block was about to run again."


def is_sentinel_text(text: str) -> bool:
    t = (text or "").strip()
    return (not t) or t == BUDGET_EXHAUSTED_SENTINEL or t.startswith(STALL_TEXT_PREFIX)


def _scalar(value: Any) -> Any:
    """Unwrap numpy/pandas scalars and size-1 containers; return the value unchanged otherwise."""
    if hasattr(value, "item") and getattr(value, "size", 1) == 1:
        try:
            return value.item()
        except Exception:  # noqa: BLE001
            return value
    return value


def kind_of(value: Any) -> str:
    """'unset' | 'number' | 'nonfinite' | 'text' | 'other'."""
    if value is UNSET:
        return "unset"
    v = _scalar(value)
    if isinstance(v, bool):
        return "other"
    if isinstance(v, (int, float)):
        return "number" if math.isfinite(v) else "nonfinite"
    if isinstance(v, str):
        return "text"
    return "other"


@dataclass(frozen=True)
class Decision:
    accept: bool
    reason: str


def resolve(pre: Any, post: Any) -> Decision:
    """Decide whether a verifier's value for one variable replaces the staged value."""
    pk, qk = kind_of(pre), kind_of(post)
    if _same(pre, post):
        return Decision(True, "unchanged")
    if qk == "unset":
        # The verifier removed the value: the supported way to say "unanswerable".
        return Decision(True, "verifier unset the variable")
    if qk == "nonfinite":
        return Decision(False, "verifier wrote NaN/inf")
    if qk == "other":
        return Decision(False, "verifier wrote a non-scalar value")
    if qk == "text" and pk in {"number", "nonfinite"}:
        return Decision(False, "verifier wrote text over a numeric value")
    return Decision(True, "valid value")


def _same(a: Any, b: Any) -> bool:
    if a is UNSET and b is UNSET:
        return True
    if a is UNSET or b is UNSET:
        return False
    try:
        return bool(_scalar(a) == _scalar(b))
    except Exception:  # noqa: BLE001
        return repr(a) == repr(b)
