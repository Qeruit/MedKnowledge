"""Continuous monitor + persisted due-time backups: python -m app.ops_agent."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import signal
import threading
from datetime import datetime
from sqlalchemy import create_engine,text
from sqlalchemy.pool import NullPool

from app.config import settings,PROJECT_ROOT
from app.operations import snapshot,evaluate
from app.backups import backup_due,create_backup,prune_backups
from app.chat_history import prune_expired_conversations

logger=logging.getLogger(__name__)


def save_runtime(state,path=None):
    target=path or PROJECT_ROOT/"storage"/"ops-runtime.json"
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"at":datetime.now().isoformat(),**state},ensure_ascii=False,indent=2),encoding="utf-8")
    os.replace(temporary,target)


def monitor_tick(runtime_path=None):
    target=runtime_path or PROJECT_ROOT/"storage"/"ops-runtime.json"
    previous={}
    try: previous=json.loads(target.read_text(encoding="utf-8"))
    except (OSError,ValueError): pass
    try:
        data=snapshot()
        changes=evaluate(data)
        expired_conversations = prune_expired_conversations()
        if expired_conversations:
            logger.info("Pruned %s expired chat conversations", expired_conversations)
        if previous.get("databaseAvailable") is False:
            from app.db import db_session
            change={"rule":"database_unavailable","state":"resolved","detail":"监控数据库连接恢复"}
            with db_session() as db:
                db.execute(text("""INSERT INTO t_ops_alert_history(rule,state,detail)
                    VALUES(:rule,:state,:detail)"""),change)
            changes.append(change)
            logger.info("OPS_ALERT database_unavailable resolved")
        save_runtime({"databaseAvailable":True,"transitions":changes},runtime_path)
        return changes
    except Exception:
        # DB outage cannot be persisted IN that DB; local fallback stays secret-free.
        save_runtime({"databaseAvailable":False,"alerts":[{"rule":"database_unavailable","state":"firing"}]},runtime_path)
        logger.error("OPS_ALERT database_unavailable")
        return [{"rule":"database_unavailable","state":"firing"}]


def backup_tick():
    from app.db import settings as selected
    engine=create_engine(selected.database_url,poolclass=NullPool,connect_args={"connect_timeout":5})
    try:
        with engine.connect() as conn:
            acquired=conn.scalar(text("SELECT pg_try_advisory_lock(hashtext('medknowledge-scheduled-backup'))"))
            conn.commit()
            if not acquired: return None
            try:
                if backup_due():
                    result=create_backup()
                    removed=prune_backups()
                    logger.info("Backup complete id=%s bytes=%s; pruned=%s",result["id"],result["bytes"],removed)
                    return result
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(hashtext('medknowledge-scheduled-backup'))")); conn.commit()
    finally: engine.dispose()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once",action="store_true")
    parser.add_argument("--no-backup",action="store_true",help="Only inspect alerts; do not schedule backup")
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    stopping=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM): signal.signal(sig,lambda *_: stopping.set())
    future=None
    with ThreadPoolExecutor(max_workers=1,thread_name_prefix="scheduled-backup") as pool:
        while not stopping.is_set():
            monitor_tick()
            if future and future.done():
                try: future.result()
                except Exception: logger.error("Scheduled backup failed; inspect backup records and local runtime status")
                future=None
            if not args.no_backup and future is None: future=pool.submit(backup_tick)
            if args.once:
                if future: future.result()
                monitor_tick()
                break
            stopping.wait(settings.monitor_interval_seconds)


if __name__=="__main__": main()
