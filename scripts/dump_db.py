"""Dump the ingested database without putting admin credentials in the image.

Usage:
    uv run python scripts/dump_db.py

The admin DSN is read from $NEURO_ADMIN_ENV or ~/.neuro-air/admin.env. The
output is a versioned pg_dump custom-format file under backups/ (gitignored).
"""

from __future__ import annotations

import argparse
import os
import subprocess
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def admin_dsn() -> str:
    path = Path(os.environ.get("NEURO_ADMIN_ENV", Path.home() / ".neuro-air" / "admin.env"))
    values = dotenv_values(path)
    dsn = values.get("DB_URL_ADMIN") or os.environ.get("DB_URL_ADMIN")
    if not dsn:
        raise SystemExit(f"DB_URL_ADMIN not found in {path} or the environment")
    return dsn.replace("postgresql+psycopg2://", "postgresql://", 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="destination .dump file (default: backups/neuroair-<UTC timestamp>.dump)",
    )
    args = parser.parse_args()
    destination = args.output or ROOT / "backups" / f"neuroair-{datetime.utcnow():%Y%m%dT%H%M%SZ}.dump"
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file",
        str(destination),
        admin_dsn(),
    ]
    subprocess.run(command, check=True)
    print(f"wrote {destination}")


if __name__ == "__main__":
    main()
