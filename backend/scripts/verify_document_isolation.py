"""Run synthetic scope/permission integration tests in a new disposable DB only."""
import os
import re
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.config import PROJECT_ROOT, settings


def main():
    name = "agent_customer_service_scope_" + uuid4().hex[:12]
    if not re.fullmatch(r"agent_customer_service_scope_[a-f0-9]{12}", name):
        raise RuntimeError("Invalid disposable database name")
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
        # Dedicated child process imports all settings against the empty test DB.
        env = {**os.environ, "PROJECT_DB_NAME": name, "SCOPE_TEST_DB": name, "RUN_UPLOAD_DB_TEST": "1"}
        selected = " or ".join((
            "test_retrieval_uses_only_granted_published_sources_and_revoke_is_immediate",
            "test_selected_document_scopes_vector_lexical_sources_and_answer",
            "test_durable_followup_is_file_scoped_and_never_uses_old_answer_as_evidence",
            "test_version_approval_requires_independent_reviewer"))
        # Fail closed if a local dotenv override points the child back to a real
        # database. The assertion runs before pytest imports any test fixtures.
        launch = ("import os,sys; from app.config import settings; "
                  "assert settings.database_name == os.environ['SCOPE_TEST_DB'], 'Test DB override rejected'; "
                  "import pytest; sys.exit(pytest.main(sys.argv[1:]))")
        result = subprocess.run([sys.executable, "-c", launch, "tests/test_permissions.py", "-q",
                                 "-p", "no:cacheprovider", "-k", selected],
                                cwd=PROJECT_ROOT, env=env, check=False)
        code = result.returncode
    finally:
        if target is not None:
            target.dispose()
        if created:
            with admin.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
            print("Disposable scope database removed; source documents were not copied or modified.", flush=True)
        admin.dispose()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
