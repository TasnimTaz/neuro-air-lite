"""Restore a dump made by scripts/dump_db.py into an empty PostGIS database.

This is the no-re-ingest path for a new machine. It is destructive: objects in
the target database are dropped and recreated. It never runs init_db.py --reset.

Usage:
    uv run python scripts/restore_db.py backups/neuroair-....dump --yes
"""

from __future__ import annotations

import argparse
import os
import subprocess
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
    parser.add_argument("dump", type=Path, help="custom-format dump from dump_db.py")
    parser.add_argument(
        "--yes", action="store_true", help="confirm that target objects may be dropped"
    )
    args = parser.parse_args()
    if not args.dump.is_file():
        raise SystemExit(f"dump not found: {args.dump}")
    if not args.yes:
        raise SystemExit("refusing to overwrite target objects without --yes")

    command = [
        "pg_restore",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--dbname",
        admin_dsn(),
        str(args.dump),
    ]
    subprocess.run(command, check=True)
    print(f"restored {args.dump}")
    print("next: uv run python scripts/migrate_views.py")


if __name__ == "__main__":
    main()
