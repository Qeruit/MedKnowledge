"""Create only this product's dedicated database and idempotent schema."""

from pathlib import Path
import re

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import PROJECT_ROOT, settings


def main():
    name = settings.database_name
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", name):
        raise SystemExit("PROJECT_DB_NAME 只能使用小写字母、数字和下划线")
    source_url = make_url(settings.source_database_url)
    if name == source_url.database or name in {"postgres", "template0", "template1"}:
        raise SystemExit("拒绝对原数据库或系统数据库执行初始化")
    admin_url = source_url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        exists = conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name})
        if not exists:
            conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
            print(f"Created dedicated database: {name}")
        else:
            print(f"Dedicated database already exists: {name}")
    admin_engine.dispose()

    schema = (PROJECT_ROOT / "schema.sql").read_text(encoding="utf-8")
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        conn.exec_driver_sql(schema)
    engine.dispose()
    from app.task_queue import backfill_pending
    recovered = backfill_pending()
    print(f"Pending file tasks reconciled: {recovered}")
    print("Knowledge schema ready; original database untouched.")


if __name__ == "__main__":
    main()
