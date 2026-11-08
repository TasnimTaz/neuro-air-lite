"""Day 0 environment check for neuro-air-lite.

    uv run python scripts/check_env.py            # DB/LLM down -> SKIP, exit 0
    uv run python scripts/check_env.py --strict   # any SKIP becomes a failure

Checks, in order:
  1. interpreter version and venv
  2. every dependency imports
  3. .env exists and has the required keys
  4. the LLM endpoint answers /models and serves LLM_MODEL
  5. the database answers and reports its PostGIS version
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LIBS = [
    ("openai", "openai"),
    ("sqlalchemy", "sqlalchemy"),
    ("psycopg2", "psycopg2-binary"),
    ("pandas", "pandas"),
    ("numpy", "numpy"),
    ("scipy", "scipy"),
    ("shapely", "shapely"),
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("apscheduler", "apscheduler"),
    ("httpx", "httpx"),
    ("dotenv", "python-dotenv"),
    ("pytest", "pytest"),
    ("statsmodels", "statsmodels"),
]

REQUIRED_ENV = ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "DB_URL_ADMIN"]

MARK = {"ok": "[ ok ]", "warn": "[skip]", "fail": "[FAIL]"}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))
        print(f"{MARK[status]} {name:<32} {detail}")

    def count(self, status: str) -> int:
        return sum(1 for s, _, _ in self.rows if s == status)


def check_python(rep: Report) -> None:
    v = sys.version_info
    detail = f"{v.major}.{v.minor}.{v.micro}  {sys.executable}"
    if (v.major, v.minor) != (3, 12):
        rep.add("fail", "python 3.12", detail)
    elif ".venv" not in sys.executable:
        rep.add("warn", "python 3.12", detail + "  (not the project venv?)")
    else:
        rep.add("ok", "python 3.12", detail)


def check_libs(rep: Report) -> None:
    missing = []
    for module, dist in LIBS:
        try:
            mod = importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - report anything import-time
            missing.append(f"{dist} ({exc.__class__.__name__})")
            continue
        version = getattr(mod, "__version__", "")
        print(f"[ ok ] {dist:<32} {version}")
    if missing:
        rep.add("fail", "dependencies", ", ".join(missing))
    else:
        rep.add("ok", "dependencies", f"{len(LIBS)}/{len(LIBS)} importable")


def check_env_file(rep: Report) -> bool:
    env_path = ROOT / ".env"
    if not env_path.exists():
        rep.add("fail", ".env", f"missing -> copy .env.example to .env")
        return False

    try:
        from dotenv import load_dotenv
    except Exception:  # noqa: BLE001
        rep.add("fail", ".env", "python-dotenv not importable")
        return False

    load_dotenv(env_path, override=True)
    absent = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if absent:
        rep.add("fail", ".env", f"missing keys: {', '.join(absent)}")
        return False

    rep.add("ok", ".env", f"LLM_MODEL={os.environ['LLM_MODEL']}")
    return True


def check_llm(rep: Report) -> None:
    import httpx

    base = os.environ["LLM_BASE_URL"].rstrip("/")
    key = os.environ["LLM_API_KEY"]
    want = os.environ["LLM_MODEL"]
    try:
        resp = httpx.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=5.0,
        )
        resp.raise_for_status()
        served = [m.get("id", "") for m in resp.json().get("data", [])]
    except Exception as exc:  # noqa: BLE001
        rep.add("warn", "llm endpoint", f"{base} unreachable ({exc.__class__.__name__})")
        return

    if want in served:
        rep.add("ok", "llm endpoint", f"{base}  model ready: {want}")
    else:
        rep.add(
            "warn",
            "llm endpoint",
            f"{base} up, but '{want}' not served (have: {', '.join(served) or 'none'})",
        )

    if not os.environ.get("LLM_MODEL_STRONG"):
        print("       note: LLM_MODEL_STRONG empty - fine for dev, needed for Day 9 eval")


def check_db(rep: Report) -> None:
    from sqlalchemy import create_engine, text

    url = os.environ["DB_URL_ADMIN"]
    engine = create_engine(
        url,
        connect_args={"connect_timeout": 3},
        pool_pre_ping=True,
    )
    try:
        with engine.connect() as conn:
            postgis = conn.execute(text("SELECT PostGIS_Version()")).scalar_one()
            db = conn.execute(text("SELECT current_database()")).scalar_one()
    except Exception as exc:  # noqa: BLE001
        rep.add("warn", "database", f"not reachable yet ({exc.__class__.__name__})")
        return
    finally:
        engine.dispose()

    rep.add("ok", "database", f"{db}  PostGIS {postgis}")


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    rep = Report()
    print("=== neuro-air-lite :: environment check ===\n")

    check_python(rep)
    check_libs(rep)
    print()
    if check_env_file(rep):
        check_llm(rep)
        check_db(rep)

    fails, warns = rep.count("fail"), rep.count("warn")
    print()
    if fails or (strict and warns):
        print(f"RESULT: {fails} failed, {warns} skipped")
        return 1
    print(f"RESULT: ready  ({warns} thing(s) skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
