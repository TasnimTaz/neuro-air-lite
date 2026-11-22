"""v5 role prompts: identical procedures, but the answer contract is named variables.

v2-v4 scored one ``ANSWER`` variable plus the final sentence. The paper's
programmatic validators instead read task-specific *stored variables* from the
runtime (e.g. ``net_mean_pm25``) and never look at prose. That removes a whole
class of multi-agent-only failures that are about the hand-off of a single
scalar and its sentence, not about analysis (stale ANSWER, string-in-numeric
ANSWER, synthesis reporting a different staged value).

This module derives the v5 prompts from ``roles.py`` by a mechanical, tested
substitution so the v4 procedures stay the single source of truth:

* ``ANSWER`` becomes "the required output variables listed in the task";
* nothing else is edited (no new schema facts, no worked examples).

Both arms must use these (the single arm through ``MERGED_ROLE_ADDENDUM_V5``).
"""

from __future__ import annotations

from domains.hk import roles as _v4

_SPECIAL = (
    (
        "Assign the required final variable (for this task, ANSWER) exactly once, print it with its unit,",
        "Assign each required output variable listed in the task exactly once, print each with its unit,",
    ),
    (
        "Only synthesis_report may assign the final required output, ANSWER.",
        "Only synthesis_report may assign the required output variables listed in the task.",
    ),
)


def _convert(text: str) -> str:
    for old, new in _SPECIAL:
        text = text.replace(old, new)
    text = text.replace("ANSWER", "the required output variables")
    assert "ANSWER" not in text
    return text


COORDINATOR_SYSTEM = _convert(_v4.COORDINATOR_SYSTEM)
ROLE_PROMPTS = {role: _convert(prompt) for role, prompt in _v4.ROLE_PROMPTS.items()}
MULTI_RULES = _convert(_v4.MULTI_RULES)


def merged_role_addendum() -> str:
    roles = "\n\n".join(f"PROCEDURE {role}:\n{prompt}" for role, prompt in ROLE_PROMPTS.items())
    return (
        "<merged_role_procedures>\n"
        "You are one agent performing the following procedures yourself, in a\n"
        "single persistent runtime. Treat every worker role below as a procedural\n"
        "check you may perform; you do not have separate workers or a coordinator.\n\n"
        f"{MULTI_RULES}\n\n{roles}\n"
        "</merged_role_procedures>"
    )


MERGED_ROLE_ADDENDUM_V5 = merged_role_addendum()
