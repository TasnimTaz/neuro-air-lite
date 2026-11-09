"""Prompt rendering tests.

`SYSTEM_PROMPT` is a `str.format` template, so every literal brace in the prompt
must be doubled. A missing escape surfaced as `KeyError: '"station"'` at runtime
and, without these tests, only when the first live question ran.
"""

import pytest
from sqlalchemy.exc import OperationalError

from core.prompts import SCHEMA, get_system_prompt, render_schema


def test_prompt_renders_without_engine():
    prompt = get_system_prompt()

    # Literal JSON-ish examples survived .format().
    assert '"station": "Tai Po"' in prompt
    assert "Bind parameters" in prompt

    # Enum labels and units come from the curated schema, not introspection.
    assert "'GENERAL' | 'ROADSIDE'" in prompt
    assert "ug/m3" in prompt
    assert "ERA5 reanalysis" in prompt
    assert "HKO 10-minute observations" in prompt
    assert "(Geometry, 4326)" in prompt

    # Rules that prevent silent wrong answers.
    assert "NULL means" in prompt
    assert "Asia/Hong_Kong" in prompt
    assert "Data coverage" not in prompt  # only injected with a live engine
    assert "DB_URL_ADMIN" not in prompt


def test_schema_lists_every_exposed_table():
    rendered = render_schema()
    for table in SCHEMA:
        assert f"Table {table}:" in rendered
    assert "Table land_use_zone" not in rendered


def test_prompt_injects_live_coverage_and_inventory():
    from domains.hk.tools import build_runtime

    try:
        runtime = build_runtime()
    except Exception as error:  # noqa: BLE001
        pytest.skip(f"database not reachable: {error}")

    try:
        prompt = get_system_prompt(engine=runtime.ns["engine"])
    except OperationalError as error:
        pytest.skip(f"database not reachable: {error.orig}")
    finally:
        runtime.close()

    assert "hourly_station_air_quality.datetime: 2025-01-01 00:00:00" in prompt
    assert "Tai Po [taipo] GENERAL" in prompt
    assert "The database is historical only" in prompt
