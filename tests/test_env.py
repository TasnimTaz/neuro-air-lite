"""Tests for the two-file environment split.

The repo `.env` is model-readable (pandas can read it even when `open` is
blocked). The admin DSN and POSTGRES_* therefore live outside the repository.
These tests pin both halves of that claim.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import core.env as env_module
from core.env import admin_env_path, admin_url

ROOT = Path(__file__).resolve().parents[1]


def test_repo_dotenv_has_no_admin_secrets():
    """A model-reachable read of the repo .env must not expose the admin DSN."""
    path = ROOT / ".env"
    if not path.exists():
        pytest.skip(".env is absent on a clean checkout")

    text = path.read_text(encoding="utf-8")
    for key in env_module.ADMIN_ENV_KEYS:
        assert f"{key}=" not in text, f"{key} must not be assigned in the repo .env"
    assert "POSTGRES_PASSWORD=" not in text


def test_admin_env_path_is_outside_the_repository(monkeypatch):
    monkeypatch.delenv("NEURO_ADMIN_ENV", raising=False)
    path = admin_env_path()
    assert path.name == "admin.env"
    assert ROOT not in path.parents


def test_admin_env_path_override(monkeypatch, tmp_path):
    override = tmp_path / "admin.env"
    monkeypatch.setenv("NEURO_ADMIN_ENV", str(override))
    assert admin_env_path() == override


def test_admin_url_reads_the_admin_file_not_the_repo_dotenv(monkeypatch, tmp_path):
    admin_file = tmp_path / "admin.env"
    admin_file.write_text(
        "DB_URL_ADMIN=postgresql+psycopg2://admin:secret@127.0.0.1:5432/airdb\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NEURO_ADMIN_ENV", str(admin_file))
    monkeypatch.delenv("DB_URL_ADMIN", raising=False)
    # If admin_url() ever consults the repo .env, this sentinel would leak out.
    monkeypatch.setattr(
        env_module,
        "_file_values",
        lambda: {"DB_URL_ADMIN": "postgresql+psycopg2://sentinel@repo/db"},
    )

    assert admin_url() == "postgresql+psycopg2://admin:secret@127.0.0.1:5432/airdb"


def test_load_admin_env_exports_the_admin_file(monkeypatch, tmp_path):
    admin_file = tmp_path / "admin.env"
    admin_file.write_text("DB_URL_ADMIN=postgresql+psycopg2://admin:secret@h/db\n", encoding="utf-8")
    monkeypatch.setenv("NEURO_ADMIN_ENV", str(admin_file))
    monkeypatch.delenv("DB_URL_ADMIN", raising=False)

    loaded = env_module.load_admin_env(override=True)

    assert loaded == admin_file
    assert os.environ["DB_URL_ADMIN"].endswith("/db")
