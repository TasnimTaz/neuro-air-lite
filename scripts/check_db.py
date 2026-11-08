"""Day 1 acceptance checks for the Hong Kong database.

    uv run python scripts/check_db.py

Verifies that the schema matches the expected table list, that every table is
reachable by the read-only role, and that the read-only role really cannot write
or change structure. Exits non-zero on any failure.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
ROLE = "neuro_ro"
STATEMENT_TIMEOUT = "5s"
STATEMENT_TIMEOUT_SETTING = "5000ms"

EXPECTED_TABLES = [
    "air_quality_station",
    "hourly_station_air_quality",
    "street",
    "road_segment",
    "hourly_road_segment_traffic",
    "weather_station",
    "hourly_station_weather",
    "osm_feature",
    "eia_project",
    "eia_habitat",
    "traffic_camera",
    "sensitive_area",
    "land_use_zone",
    "building_height_control",
]

# (label, statement) - every one of these must be rejected by neuro_ro.
FORBIDDEN = [
    ("INSERT", "INSERT INTO air_quality_station (name_en) VALUES ('test')"),
    ("UPDATE", "UPDATE air_quality_station SET name_en = 'test'"),
    ("DELETE", "DELETE FROM air_quality_station"),
    ("TRUNCATE", "TRUNCATE air_quality_station"),
    ("DROP TABLE", "DROP TABLE air_quality_station"),
    ("ALTER TABLE", "ALTER TABLE air_quality_station ADD COLUMN nope integer"),
    ("CREATE TABLE", "CREATE TABLE should_fail (id integer)"),
    ("CREATE INDEX", "CREATE INDEX ON air_quality_station (name_en)"),
]

failures: list[str] = []


def ok(msg: str) -> None:
    print(f"[ ok ] {msg}")


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}")
    failures.append(msg)


def warn(msg: str) -> None:
    print(f"[warn] {msg}")


def pgcode(exc: Exception) -> str | None:
    return getattr(getattr(exc, "orig", None), "pgcode", None)


def check_admin(admin_url: str) -> None:
    engine = create_engine(admin_url, future=True)
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT PostGIS_Version()")).scalar_one()
            ok(f"PostGIS {version}")

            tables = {
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                    )
                )
            }
            missing = [t for t in EXPECTED_TABLES if t not in tables]
            extra = sorted(tables - set(EXPECTED_TABLES))
            if missing:
                fail(f"missing tables: {', '.join(missing)}")
            else:
                ok(f"all {len(EXPECTED_TABLES)} expected tables present")
            if extra:
                print(f"       note: extra tables present: {', '.join(extra)}")

            empty = [
                t
                for t in EXPECTED_TABLES
                if conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar_one() == 0
            ]
            ok(f"{len(empty)}/{len(EXPECTED_TABLES)} tables are empty (Day 1: nothing ingested yet)")

            row = conn.execute(
                text(
                    "SELECT rolsuper, rolcanlogin FROM pg_roles WHERE rolname = :name"
                ),
                {"name": ROLE},
            ).first()
            if row is None:
                fail(f"role {ROLE} does not exist")
                return
            superuser, canlogin = row
            if superuser:
                fail(f"{ROLE} is a superuser")
            if not canlogin:
                fail(f"{ROLE} cannot log in")
            if not superuser and canlogin:
                ok(f"{ROLE} exists, not a superuser, can log in")

            role_settings = conn.execute(
                text(
                    "SELECT d.setconfig FROM pg_db_role_setting d "
                    "JOIN pg_roles r ON r.oid = d.setrole "
                    "WHERE r.rolname = :name"
                ),
                {"name": ROLE},
            ).scalars().all()
            flattened = [item for group in role_settings for item in group]
            wanted = {
                "default_transaction_read_only=on",
                # stored as written by ALTER ROLE, SHOW renders it as "5s"
                f"statement_timeout={STATEMENT_TIMEOUT_SETTING}",
            }
            if wanted <= set(flattened):
                ok(f"role defaults applied: {', '.join(sorted(wanted))}")
            else:
                fail(f"role defaults incomplete, have: {flattened}")
    finally:
        engine.dispose()


def check_readonly(ro_url: str) -> None:
    engine = create_engine(
        ro_url,
        future=True,
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=5000"},
    )
    try:
        with engine.connect() as conn:
            user = conn.execute(text("SELECT current_user")).scalar_one()
            db = conn.execute(text("SELECT current_database()")).scalar_one()
            read_only = conn.execute(text("SHOW default_transaction_read_only")).scalar_one()
            timeout = conn.execute(text("SHOW statement_timeout")).scalar_one()
            ok(f"connected as {user} to {db}")
            if read_only.lower() != "on":
                fail(f"default_transaction_read_only is '{read_only}', expected on")
            else:
                ok(f"default_transaction_read_only={read_only}")
            if timeout != STATEMENT_TIMEOUT:
                fail(f"statement_timeout is '{timeout}', expected {STATEMENT_TIMEOUT}")
            else:
                ok(f"statement_timeout={timeout}")

            for table in EXPECTED_TABLES:
                try:
                    conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()
                except Exception as exc:  # noqa: BLE001
                    fail(f"cannot SELECT from {table}: {exc.__class__.__name__}")
            ok(f"SELECT allowed on all {len(EXPECTED_TABLES)} tables")

            for label, statement in FORBIDDEN:
                conn.rollback()
                try:
                    conn.exec_driver_sql(statement)
                except Exception as exc:  # noqa: BLE001
                    ok(f"{label} rejected ({pgcode(exc) or exc.__class__.__name__})")
                else:
                    fail(f"{label} was ALLOWED for {ROLE}")
                finally:
                    conn.rollback()

            # Everything above failed at 25006 (read-only transaction) before
            # privileges were consulted, so it proves the session default but not
            # the GRANT layer. Lift the default and confirm the grants refuse too.
            conn.exec_driver_sql("SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE")
            conn.commit()
            for label, statement in (
                ("INSERT", "INSERT INTO air_quality_station (name_en) VALUES ('test')"),
                ("DELETE", "DELETE FROM air_quality_station"),
                ("CREATE TABLE", "CREATE TABLE should_fail (id integer)"),
            ):
                try:
                    conn.exec_driver_sql(statement)
                except Exception as exc:  # noqa: BLE001
                    code = pgcode(exc)
                    if code == "42501":
                        ok(f"{label} rejected by grants with read-only lifted (42501)")
                    else:
                        fail(f"{label} rejected with {code}, expected 42501 (grant layer)")
                else:
                    fail(f"{label} was ALLOWED once read-only was lifted")
                finally:
                    conn.rollback()
    finally:
        engine.dispose()


def check_timeout(ro_url: str) -> None:
    """Prove the 5 s cap is enforced, and check the role cannot lift it itself."""
    engine = create_engine(ro_url, future=True, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as conn:
            started = time.perf_counter()
            try:
                conn.exec_driver_sql("SELECT pg_sleep(6)")
            except Exception as exc:  # noqa: BLE001
                elapsed = time.perf_counter() - started
                code = pgcode(exc)
                if code == "57014" and elapsed < 8.0:
                    ok(f"pg_sleep(6) cancelled after {elapsed:.1f}s (57014)")
                else:
                    fail(f"pg_sleep(6) failed with {code} after {elapsed:.1f}s, expected 57014")
            else:
                fail("pg_sleep(6) was NOT cancelled, statement_timeout is not enforced")
            finally:
                conn.rollback()

            # statement_timeout is a user-settable GUC: the role can turn it off.
            # No GRANT can prevent that, so this is a warning, not a failure.
            try:
                conn.exec_driver_sql("SET statement_timeout = 0")
                conn.commit()
                started = time.perf_counter()
                conn.exec_driver_sql("SELECT pg_sleep(6)")
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                ok(f"{ROLE} could not disable statement_timeout ({pgcode(exc) or exc})")
                conn.rollback()
            else:
                warn(
                    "role can disable its own statement_timeout "
                    f"(pg_sleep(6) ran {time.perf_counter() - started:.1f}s after SET 0); "
                    "the real guarantee is the Day 10 process wall-clock kill"
                )
    finally:
        engine.dispose()


def check_postgis(ro_url: str) -> None:
    """Geography columns must give metres; ::geometry would silently give degrees."""
    engine = create_engine(ro_url, future=True, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as conn:
            point = "ST_GeogFromText('SRID=4326;POINT({lon} {lat})')"
            metres = conn.execute(
                text(
                    "SELECT ST_Distance("
                    + point.format(lon=114.1754, lat=22.4468)
                    + ", "
                    + point.format(lon=114.1754, lat=22.4568)
                    + ")"
                )
            ).scalar_one()
            degrees = conn.execute(
                text(
                    "SELECT ST_Distance("
                    "ST_GeogFromText('SRID=4326;POINT(114.1754 22.4468)')::geometry, "
                    "ST_GeogFromText('SRID=4326;POINT(114.1754 22.4568)')::geometry)"
                )
            ).scalar_one()
            if 1000.0 < metres < 1200.0:
                ok(f"geography distance in metres ({metres:.0f} m for 0.01 deg latitude)")
            else:
                fail(f"geography distance looks wrong: {metres}")
            if degrees < 1.0:
                ok(f"::geometry cast returns degrees ({degrees:.4f}) - prompt must forbid it")
            else:
                fail(f"::geometry cast did not return degrees: {degrees}")
    finally:
        engine.dispose()


def main() -> int:
    load_dotenv(ROOT / ".env", override=True)
    admin_url = os.environ["DB_URL_ADMIN"]
    ro_url = os.environ["DB_URL_RO"]

    print("=== neuro-air-lite :: Day 1 database check ===\n")
    print("-- admin --")
    check_admin(admin_url)
    print("\n-- read-only role --")
    check_readonly(ro_url)

    print("\n-- timeouts --")
    check_timeout(ro_url)

    print("\n-- PostGIS --")
    check_postgis(ro_url)

    print()
    if failures:
        print(f"RESULT: {len(failures)} check(s) failed")
        return 1
    print("RESULT: database ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
