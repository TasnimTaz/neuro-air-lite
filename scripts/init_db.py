"""Create the Hong Kong schema and the read-only agent role.

    uv run python scripts/init_db.py            # create what is missing
    uv run python scripts/init_db.py --reset    # drop schema public first

The agent runtime only ever connects with the `neuro_ro` role. It gets SELECT on
every table, is forced into read-only transactions by default, and has a 5 s
statement timeout so a runaway query cannot pin the database.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.sql.expression import literal

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILE = ROOT / "domains" / "hk" / "schema.sql"

ROLE = "neuro_ro"
STATEMENT_TIMEOUT = "5000ms"


def configure_role(admin_url: str, ro_url: str) -> None:
    url = make_url(ro_url)
    if url.username != ROLE:
        raise SystemExit(
            f"DB_URL_RO must connect as '{ROLE}', got '{url.username}'"
        )
    password = url.password or ""
    database = make_url(admin_url).database

    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
    try:
        with engine.connect() as conn:
            quoted = literal(password).compile(
                dialect=conn.dialect, compile_kwargs={"literal_binds": True}
            )
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": ROLE}
            ).scalar()

            if exists:
                conn.exec_driver_sql(f'ALTER ROLE "{ROLE}" WITH LOGIN PASSWORD {quoted}')
                print(f"[ ok ] role {ROLE} already existed, password refreshed")
            else:
                conn.exec_driver_sql(f'CREATE ROLE "{ROLE}" WITH LOGIN PASSWORD {quoted}')
                print(f"[ ok ] role {ROLE} created")

            statements = [
                f'GRANT CONNECT ON DATABASE "{database}" TO "{ROLE}"',
                f'GRANT USAGE ON SCHEMA public TO "{ROLE}"',
                f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{ROLE}"',
                f'GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO "{ROLE}"',
                f'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES '
                f'IN SCHEMA public FROM "{ROLE}"',
                f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                f'GRANT SELECT ON TABLES TO "{ROLE}"',
                f'ALTER ROLE "{ROLE}" SET default_transaction_read_only = on',
                f'ALTER ROLE "{ROLE}" SET statement_timeout = {literal(STATEMENT_TIMEOUT).compile(dialect=conn.dialect, compile_kwargs={"literal_binds": True})}',
            ]
            for sql in statements:
                conn.exec_driver_sql(sql)
            print(f"[ ok ] granted SELECT-only access on {database}.public")
            print(f"[ ok ] default_transaction_read_only=on, statement_timeout={STATEMENT_TIMEOUT}")
    finally:
        engine.dispose()


def apply_schema(admin_url: str, reset: bool) -> None:
    ddl = SCHEMA_FILE.read_text(encoding="utf-8")
    engine = create_engine(admin_url, future=True)
    try:
        with engine.begin() as conn:
            if reset:
                conn.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
                conn.exec_driver_sql("CREATE SCHEMA public")
                print("[ ok ] schema public recreated")
            conn.exec_driver_sql(ddl)
            tables = conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                )
            ).scalar_one()
        print(f"[ ok ] schema applied, {tables} tables in public")
    finally:
        engine.dispose()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="drop schema public CASCADE first")
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env", override=True)
    admin_url = os.environ.get("DB_URL_ADMIN")
    ro_url = os.environ.get("DB_URL_RO")
    if not admin_url or not ro_url:
        print("DB_URL_ADMIN and DB_URL_RO must be set in .env", file=sys.stderr)
        return 2

    try:
        apply_schema(admin_url, args.reset)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] schema apply failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        print("       if it already exists, rerun with --reset", file=sys.stderr)
        return 1

    try:
        configure_role(admin_url, ro_url)
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] role setup failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
