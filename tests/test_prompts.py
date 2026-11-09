"""Prompt rendering tests.

The prompt is assembled by concatenation, so literal braces in JSON examples are
not a format hazard. The Day 3 version used `str.format` and one missing escape
surfaced as `KeyError: '"station"'` at runtime; `test_literal_braces_survive`
keeps that failure from coming back.
"""

import pytest
from sqlalchemy.exc import OperationalError

from domains.hk.schema_prompt import SCHEMA, get_system_prompt, render_schema


def test_prompt_renders_without_engine():
    prompt = get_system_prompt()

    # Literal JSON-ish examples survived assembly without brace escaping.
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


def test_prompt_includes_knowledge_base_and_tools():
    prompt = get_system_prompt()

    # AQHI top band and its database encoding.
    assert "10+ SERIOUS" in prompt
    assert "`aqhi = 11`" in prompt

    # Both AQO regimes, because the 2025 window crosses 2025-04-11.
    assert "From 2025-04-11" in prompt
    assert "From 2022-01-01 to 2025-04-10" in prompt
    assert "24h 37.5" in prompt
    assert "24h 50" in prompt

    # External standards carry a source.
    assert "9789240034228" in prompt  # WHO 2021
    assert "Historical_AQOs_Eng.pdf" in prompt

    # Tool docstrings and examples.
    assert "geocode(address)" in prompt
    assert "charts.line(" in prompt
    assert "geocode('The University of Hong Kong')" in prompt


def test_literal_braces_survive_assembly():
    prompt = get_system_prompt()
    assert '{"station": "Tai Po"}' in prompt
    assert "{{" not in prompt
    assert "}}" not in prompt


def test_prompt_size_is_bounded():
    # ~4k tokens at four characters per token. Ollama is started with a 16k
    # context so this leaves room for the conversation history.
    assert len(get_system_prompt()) < 24000


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
