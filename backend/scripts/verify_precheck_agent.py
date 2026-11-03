"""Run Agent invariants in an empty disposable PostgreSQL database. No model fees."""
import os
import argparse
import re
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from app.config import PROJECT_ROOT, settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="Run full offline suite plus Agent DB integration")
    args = parser.parse_args()
    name = "agent_customer_service_precheck_" + uuid4().hex[:12]
    assert re.fullmatch(r"agent_customer_service_precheck_[a-f0-9]{12}", name)
    admin = create_engine(make_url(settings.database_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    target = None
    created = False
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
        created = True
        target = create_engine(make_url(settings.database_url).set(database=name))
        with target.begin() as conn:
            conn.exec_driver_sql((PROJECT_ROOT / "schema.sql").read_text(encoding="utf-8"))
        env = {**os.environ, "PROJECT_DB_NAME": name, "PRECHECK_TEST_DB": name, "RUN_PRECHECK_DB_TEST": "1"}
        launch = ("import os,sys; from app.config import settings; "
                  "assert settings.database_name==os.environ['PRECHECK_TEST_DB']; "
                  "import pytest; sys.exit(pytest.main(sys.argv[1:]))")
        completed = subprocess.run([sys.executable, "-c", launch, "tests" if args.all else "tests/test_precheck_agent.py", "-q", "-p", "no:cacheprovider"],
                                   cwd=PROJECT_ROOT, env=env, check=False)
        code = completed.returncode
    finally:
        if target is not None:
            target.dispose()
        if created:
            with admin.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
            print("Disposable precheck DB removed; no source files or approvals modified.")
        admin.dispose()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
