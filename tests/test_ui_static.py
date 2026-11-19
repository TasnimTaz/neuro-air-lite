"""Static UI safety checks.

The browser code still uses a few HTML strings for its static layout. This test
does not try to be a full JavaScript parser; it enforces the project rule that a
template literal containing markup may only interpolate an expression that is
explicitly escaped (``esc(...)``), a named ``safe...`` value, or a numeric
formatter. A negative fixture proves the scan catches the XSS shape we care
about: model/DB text inserted into an ``innerHTML`` template.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "api" / "static"

TEMPLATE_RE = re.compile(r"`([^`]*)`", re.S)
INTERPOLATION_RE = re.compile(r"\$\{([^{}]*)\}")
NUMERIC_FORMATTER_RE = re.compile(
    r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\.toLocaleString\(\)$"
)


def _is_safe_expression(expression: str) -> bool:
    expression = expression.strip()
    if expression.startswith("safe"):
        return True
    if expression.startswith("esc("):
        return True
    if NUMERIC_FORMATTER_RE.fullmatch(expression):
        return True
    if re.fullmatch(r"\d+", expression):
        return True
    return False


def find_unsafe_interpolations(source: str) -> list[tuple[int, str]]:
    """Return ``(line, expression)`` for markup templates with unsafe values."""
    violations: list[tuple[int, str]] = []
    for template_match in TEMPLATE_RE.finditer(source):
        template = template_match.group(1)
        if "<" not in template or "${" not in template:
            continue
        for interpolation in INTERPOLATION_RE.finditer(template):
            expression = interpolation.group(1).strip()
            if _is_safe_expression(expression):
                continue
            line = source.count("\n", 0, template_match.start() + interpolation.start()) + 1
            violations.append((line, expression))
    return violations


def test_known_safe_templates_are_accepted() -> None:
    source = """
      const safeLabel = esc(modelText);
      container.innerHTML = `<p>${safeLabel}</p><b>${esc(otherText)}</b>`;
    """
    assert find_unsafe_interpolations(source) == []


def test_static_scan_catches_unescaped_model_output() -> None:
    source = """
      const payload = '<img src=x onerror=alert(1)>';
      container.innerHTML = `<div>${payload}</div>`;
    """
    violations = find_unsafe_interpolations(source)
    assert violations
    assert violations[0][1] == "payload"


def test_app_js_markup_templates_escape_dynamic_values() -> None:
    source = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert find_unsafe_interpolations(source) == []


def test_index_html_has_no_dynamic_markup_templates() -> None:
    source = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert find_unsafe_interpolations(source) == []


def test_reference_ui_is_static_markup_and_text_content_only() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "Deterministic reference (no AI)" in html
    assert 'id="reference-body"' in html
    assert 'id="reference-charts"' in html
    assert 'id="reference-caveat"' in html

    # The reference renderer builds DOM nodes and assigns textContent; it must
    # not interpolate the file payload into a markup template.
    assert "referenceBody.replaceChildren()" in js
    assert "summary.textContent = " in js
    assert "referenceCaveat.textContent = " in js
    assert find_unsafe_interpolations(js) == []
