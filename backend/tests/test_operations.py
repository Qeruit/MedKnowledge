"""Monitoring and real backup tests in their own disposable database."""
import asyncio
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url

from app.config import PROJECT_ROOT,settings

pytestmark=pytest.mark.skipif(os.getenv("RUN_UPLOAD_DB_TEST")!="1",reason="需要 PostgreSQL / pg_dump")


@pytest.fixture
def ops_database(monkeypatch,tmp_path):
    from app import db,auth,main,operations,ops_agent,llm,knowledge,resumable_upload,worker,task_queue,documents
    name="agent_customer_service_ops_"+uuid4().hex[:12]
    source=make_url(settings.database_url)
    admin=create_engine(source.set(database="postgres"),isolation_level="AUTOCOMMIT")
    with admin.connect() as conn: conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    engine=create_engine(source.set(database=name))
    with engine.begin() as conn: conn.exec_driver_sql((PROJECT_ROOT/"schema.sql").read_text(encoding="utf-8"))
    chosen=replace(settings,database_name=name,md_storage_path=tmp_path/"md",backup_root=tmp_path/"backups",
                   storage_lock_timeout_seconds=1,alert_hold_seconds=0)
    if db.session_factory.cache_info().currsize:
        db.session_factory().kw["bind"].dispose(); db.session_factory.cache_clear()
    for module in (db,auth,main,operations,ops_agent,llm,knowledge,resumable_upload,worker,task_queue,documents):
        monkeypatch.setattr(module,"settings",chosen)
    try: yield chosen
    finally:
        if db.session_factory.cache_info().currsize:
            db.session_factory().kw["bind"].dispose(); db.session_factory.cache_clear()
        engine.dispose()
        with admin.connect() as conn: conn.exec_driver_sql(f'DROP DATABASE "{name}"')
        admin.dispose()


def test_monitoring_admin_only_and_sse_error_under_http_200(ops_database,admin_client,monkeypatch):
    from app import main,llm
    from app.db import db_session,User
    from app.auth import hash_password
    client,headers,_=admin_client
    assert TestClient(main.app).get("/ops/status").status_code==401
    with db_session() as db:
        db.add(User(username="ops_viewer",password_hash=hash_password("Test-password-long"),role="viewer"))
    viewer=TestClient(main.app)
    viewer.post("/auth/login",json={"username":"ops_viewer","password":"Test-password-long"})
    assert viewer.get("/ops/status").status_code==403
    async def context(*_,**__): return [{"content":"## Test\nFact","metadata":{}}]
    async def chunks(*_,**__):
        await asyncio.sleep(.04)
        yield "TOP-SECRET-ANSWER"
    monkeypatch.setattr(main,"find_context",context)
    monkeypatch.setattr(main,"open_conversation",lambda *_,**__: (uuid4(),[]))
    monkeypatch.setattr(main,"finish_conversation",lambda *_,**__: True)
    monkeypatch.setattr(llm,"_chat_chunks",chunks)
    result=client.post("/customer-service/completion",json={"message":"TOP-SECRET-QUESTION","fileId":1},headers=headers)
    assert result.status_code==200 and "TOP-SECRET-ANSWER" in result.text
    async def failure(*_,**__):
        raise RuntimeError("fault injection")
        yield
    monkeypatch.setattr(llm,"_chat_chunks",failure)
    failed=client.post("/customer-service/completion",json={"message":"SECRET","fileId":1},headers=headers)
    assert failed.status_code==200 and "event: error" in failed.text
    data=client.get("/ops/status").json()["data"]
    assert data["modelCalls"]==2 and data["modelErrors"]==1
    assert any(row["kind"]=="sse" and row["outcome"]=="success" and row["ttft"]>=.04 for row in data["events"])
    assert any(row["kind"]=="sse" and row["outcome"]=="error" for row in data["events"])
    assert "TOP-SECRET" not in json.dumps(data)
    assert "medknowledge_model_errors_window 1" in client.get("/ops/metrics").text


def test_alert_firing_resolving_and_no_duplicate_notifications(ops_database):
    from app.operations import snapshot,evaluate
    from app.observability import record
    from app.db import db_session,MarkdownFile
    from app.task_queue import enqueue,heartbeat
    with db_session() as db:
        row=MarkdownFile(original_file_name="alert.md",new_file_name="alert.md",file_path="test-only",
            file_size=1,status=0,create_time=datetime.now(),update_time=datetime.now())
        db.add(row); db.flush(); enqueue(db,row.id)
        db.execute(text("UPDATE t_index_job SET create_time=clock_timestamp()-interval '700 seconds'"))
    for _ in range(3): record("embedding","error",.1)
    fired=evaluate(snapshot())
    assert {"worker_missing","queue_stalled","model_errors"}.issubset({item["rule"] for item in fired if item["state"]=="firing"})
    assert evaluate(snapshot())==[]
    heartbeat("recovered-worker")
    with db_session() as db:
        db.execute(text("UPDATE t_index_job SET create_time=clock_timestamp()"))
        db.execute(text("DELETE FROM t_ops_event WHERE kind='embedding'"))
    record("embedding","success",.1)
    resolved=evaluate(snapshot())
    assert {"worker_missing","queue_stalled","model_errors"}.issubset({item["rule"] for item in resolved if item["state"]=="resolved"})


def test_backup_gate_blocks_worker_and_write_then_releases(ops_database,admin_client):
    from app.storage_guard import storage_lock
    from app.worker import run_once
    from fastapi import HTTPException
    client,headers,_=admin_client
    with storage_lock(exclusive=True):
        response=client.post("/customer-service/md/upload",files={"file":("gate.md",b"# test")},headers=headers)
        assert response.status_code==503
        with pytest.raises(HTTPException): run_once()
    for _ in range(2):
        response=client.post("/customer-service/md/upload",files={"file":("gate.md",b"# test")},headers=headers)
        assert response.status_code==200
    # Test-owned files are deleted directly in this test-owned database at teardown.
    from app.db import db_session
    with db_session() as db:
        db.execute(text("UPDATE t_ai_customer_service_md_storage SET owner_id=NULL"))


def test_auto_backup_due_and_hash_validation(ops_database,admin_client):
    from app.ops_agent import backup_tick
    from app.backups import backup_due,verify_backup
    from app.db import db_session
    client,headers,_=admin_client
    response=client.post("/customer-service/md/upload",files={"file":("backup.md",b"# Backup\nTest")},headers=headers)
    assert response.json()["success"] and backup_due()
    manifest=backup_tick()
    assert not backup_due()
    folder,_=verify_backup(manifest["id"])
    target=folder/"storage"/manifest["files"][0]["path"]
    target.write_bytes(target.read_bytes()+b"corruption")
    with pytest.raises(ValueError): verify_backup(manifest["id"])
    with db_session() as db: db.execute(text("UPDATE t_ai_customer_service_md_storage SET owner_id=NULL"))


def test_restore_refuses_product_or_existing_database(ops_database):
    from app.backups import restore_backup
    for name in ("robot","agent_customer_service",ops_database.database_name,"postgres"):
        with pytest.raises(ValueError): restore_backup(uuid4(),name)


def test_database_failure_fallback_does_not_expose_connection_details(ops_database,monkeypatch,tmp_path):
    from app import ops_agent
    def unavailable(): raise RuntimeError("PRIVATE-DSN-and-password")
    monkeypatch.setattr(ops_agent,"snapshot",unavailable)
    output=tmp_path/"runtime.json"
    result=ops_agent.monitor_tick(output)
    assert result[0]["rule"]=="database_unavailable"
    payload=output.read_text(encoding="utf-8")
    assert "PRIVATE" not in payload and not json.loads(payload)["databaseAvailable"]
    from app.operations import snapshot
    monkeypatch.setattr(ops_agent,"snapshot",snapshot)
    recovered=ops_agent.monitor_tick(output)
    assert any(row["rule"]=="database_unavailable" and row["state"]=="resolved" for row in recovered)
    assert json.loads(output.read_text(encoding="utf-8"))["databaseAvailable"]


def test_retention_only_removes_verified_owned_backups_and_restore_refuses_existing(ops_database,monkeypatch):
    from app import db
    from app.backups import create_backup,prune_backups,restore_backup
    first=create_backup(); second=create_backup()
    unknown=ops_database.backup_root/uuid4().hex
    unknown.mkdir(); (unknown/"unverified.txt").write_text("do not remove",encoding="utf-8")
    monkeypatch.setattr(db,"settings",replace(ops_database,backup_keep_count=1))
    assert prune_backups()==[first["id"]]
    assert (ops_database.backup_root/second["id"]).is_dir() and unknown.is_dir()
    target="agent_customer_service_restore_"+uuid4().hex[:12]
    admin=create_engine(make_url(ops_database.database_url).set(database="postgres"),isolation_level="AUTOCOMMIT")
    with admin.connect() as conn: conn.exec_driver_sql(f'CREATE DATABASE "{target}"')
    try:
        with pytest.raises(ValueError,match="已存在"):
            restore_backup(second["id"],target)
        assert not (PROJECT_ROOT/"restores"/target).exists()
    finally:
        with admin.connect() as conn: conn.exec_driver_sql(f'DROP DATABASE "{target}"')
        admin.dispose()
