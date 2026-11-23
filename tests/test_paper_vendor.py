"""Integrity checks for the vendored paper HK-12 fixtures."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from evals.paper_hk12 import VENDOR, install_shims, load_tasks


def test_vendor_commit_license_and_matrix():
    readme = (VENDOR / "README.md").read_text(encoding="utf-8")
    assert "Source commit: dbc47f0c9e2192d204a04e848b253953eb1afd72" in readme
    assert "License: MIT" in readme
    assert (VENDOR / "LICENSE").exists()
    assert (VENDOR / "pv_matrix.json").exists()


def test_vendor_hashes_match_readme():
    readme = (VENDOR / "README.md").read_text(encoding="utf-8")
    pairs = re.findall(r"^([0-9a-f]{64})  (\S+)$", readme, re.MULTILINE)
    recorded = {rel: digest for digest, rel in pairs}
    files = [
        p
        for p in VENDOR.rglob("*")
        if p.is_file() and p.name != "README.md" and "__pycache__" not in p.parts
    ]
    assert len(files) == len(pairs) == 29
    for path in files:
        rel = path.relative_to(VENDOR).as_posix()
        assert recorded.get(rel) == hashlib.sha256(path.read_bytes()).hexdigest()


def test_vendor_has_exactly_12_tasks():
    install_shims(object())  # no DB is needed to import the validator modules
    tasks = load_tasks()
    assert len(tasks) == 12
    assert all(t.validator_name.startswith("validate_") for t in tasks)
