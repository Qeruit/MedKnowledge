"""Cross-process storage writer/backup gate. PostgreSQL locks are DB-local."""
from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy import create_engine,text
from sqlalchemy.pool import NullPool

@contextmanager
def storage_lock(exclusive=False, timeout=None):
    from app.db import settings  # Follows isolated test/restore DB configuration.
    engine=create_engine(settings.database_url,poolclass=NullPool,connect_args={"connect_timeout":5})
    conn=None
    locked=False
    function="pg_advisory_lock" if exclusive else "pg_advisory_lock_shared"
    unlock="pg_advisory_unlock" if exclusive else "pg_advisory_unlock_shared"
    timeout=timeout or (settings.backup_lock_timeout_seconds if exclusive else settings.storage_lock_timeout_seconds)
    try:
        conn=engine.connect()
        conn.exec_driver_sql(f"SET lock_timeout='{int(timeout)*1000}ms'")
        try:
            conn.execute(text(f"SELECT {function}(hashtextextended('medknowledge-storage',0))"))
            conn.commit()
        except Exception as exc:
            raise HTTPException(503,"资料写入暂停或数据库不可用，请稍后重试",headers={"Retry-After":"5"}) from exc
        locked=True
        yield conn
    finally:
        if conn is not None:
            try:
                conn.rollback()
                if locked:
                    conn.execute(text(f"SELECT {unlock}(hashtextextended('medknowledge-storage',0))"))
                    conn.commit()
            finally: conn.close()
        engine.dispose()


def storage_write_guard():
    with storage_lock():
        yield
