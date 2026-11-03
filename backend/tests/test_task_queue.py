"""Real PostgreSQL queue invariants; only disposable files are touched."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import os
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text

from app import knowledge, task_queue
from app.config import PROJECT_ROOT, settings
from app.db import MarkdownFile, db_session
from app.task_queue import LeaseLost, claim_job, enqueue, fail_job, fence, heartbeat, recover_expired
from app.worker import process_job, run_once

pytestmark = pytest.mark.skipif(os.getenv("RUN_UPLOAD_DB_TEST") != "1", reason="需要独立 PostgreSQL")


@pytest.fixture
def queued_file(tmp_path, admin_client, monkeypatch):
    _, _, user = admin_client
    path = tmp_path / "queue-test.md"
    path.write_text("# Queue test\nDurable test fact, no medical instructions.", encoding="utf-8")
    with db_session() as db:
        row = MarkdownFile(original_file_name=path.name,new_file_name=path.name,file_path=str(path),
            file_size=path.stat().st_size,status=0,owner_id=user.id,
            create_time=datetime.now(),update_time=datetime.now())
        db.add(row); db.flush()
        file_id = row.id
        job_id = enqueue(db, file_id)
    monkeypatch.setattr(knowledge, "embed_texts_sync", lambda values: [
        [1.] + [0.] * (settings.embedding_dimensions - 1) for _ in values])
    try:
        yield file_id, job_id
    finally:
        with db_session() as db:
            db.execute(text("DELETE FROM t_vector_store WHERE metadata->>'mdStorageId'=:id"), {"id": str(file_id)})
            db.execute(text("DELETE FROM t_admin_audit WHERE file_id=:id"), {"id": file_id})
            db.execute(text("DELETE FROM t_ai_customer_service_md_storage WHERE id=:id"), {"id": file_id})
            db.execute(text("DELETE FROM t_worker_heartbeat WHERE worker_id LIKE 'queue-test-%'"))


def job_row(job_id):
    with db_session() as db:
        return dict(db.execute(text("SELECT * FROM t_index_job WHERE id=:id"), {"id": job_id}).mappings().one())


def make_due(job_id):
    with db_session() as db:
        db.execute(text("UPDATE t_index_job SET available_at=clock_timestamp()-interval '1 second' WHERE id=:id"), {"id": job_id})


def expire(job_id):
    with db_session() as db:
        db.execute(text("UPDATE t_index_job SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"), {"id": job_id})


def test_duplicate_enqueue_and_concurrent_claim(queued_file):
    file_id, job_id = queued_file
    with db_session() as db:
        assert enqueue(db, file_id) == job_id
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda n: claim_job(f"queue-test-{n}", file_id), range(8)))
    assert len([claim for claim in claims if claim]) == 1
    assert job_row(job_id)["attempts"] == 1


def test_enqueue_rolls_back_with_file_state(queued_file):
    file_id, job_id = queued_file
    with db_session() as db:
        db.execute(text("DELETE FROM t_index_job WHERE id=:id"), {"id": job_id})
        db.get(MarkdownFile,file_id).status = 2
    with pytest.raises(RuntimeError):
        with db_session() as db:
            enqueue(db,file_id)
            raise RuntimeError("simulate failed upload commit")
    with db_session() as db:
        assert db.scalar(text("SELECT count(*) FROM t_index_job WHERE file_id=:id"), {"id":file_id}) == 0
        assert db.get(MarkdownFile,file_id).status == 2


def test_lease_recovery_fences_stale_worker(queued_file):
    file_id, job_id = queued_file
    old = claim_job("queue-test-old", file_id)
    assert heartbeat("queue-test-old", old)
    expire(job_id)
    assert not heartbeat("queue-test-old", old)
    recover_expired(); make_due(job_id)
    new = claim_job("queue-test-new",file_id)
    assert new["attempts"] == 2 and new["claim_token"] != old["claim_token"]
    with pytest.raises(LeaseLost):
        knowledge.index_markdown(file_id,old)
    assert not fail_job(old,"stale failure")
    process_job(new,"queue-test-new")
    assert job_row(job_id)["status"] == "succeeded"
    with db_session() as db:
        indexed = db.get(MarkdownFile,file_id)
        assert indexed.status == 2
        assert indexed.embedding_model == settings.embedding_model
        assert indexed.index_config["dimensions"] == settings.embedding_dimensions
        count = db.scalar(text("SELECT count(*) FROM t_vector_store WHERE metadata->>'mdStorageId'=:id"), {"id":str(file_id)})
    with pytest.raises(LeaseLost):
        knowledge.index_markdown(file_id,new)
    with db_session() as db:
        assert db.scalar(text("SELECT count(*) FROM t_vector_store WHERE metadata->>'mdStorageId'=:id"), {"id":str(file_id)}) == count


def test_transient_failure_backoff_and_exhaustion(queued_file,monkeypatch):
    file_id,job_id = queued_file
    def unavailable(values):
        raise httpx.ConnectError("temporary network error")
    monkeypatch.setattr(knowledge,"embed_texts_sync",unavailable)
    for attempt in range(1,settings.task_max_attempts+1):
        make_due(job_id)
        assert run_once("queue-test-retry",file_id)
        row = job_row(job_id)
        assert row["attempts"] == attempt
        assert row["status"] == ("failed" if attempt == settings.task_max_attempts else "queued")
        if row["status"] == "queued":
            assert claim_job("queue-test-early",file_id) is None
    with db_session() as db:
        assert db.get(MarkdownFile,file_id).status == 3


def test_permanent_error_and_manual_retry(queued_file,admin_client):
    file_id,job_id = queued_file
    with db_session() as db:
        path = db.get(MarkdownFile,file_id).file_path
    from pathlib import Path
    Path(path).write_bytes(b"\xff\xfe")
    assert run_once("queue-test-bad",file_id)
    assert job_row(job_id)["status"] == "failed" and job_row(job_id)["attempts"] == 1
    Path(path).write_text("# Repaired\nValid test document.",encoding="utf-8")
    client,headers,_ = admin_client
    response = client.post("/customer-service/md/retry",json={"id":file_id},headers=headers)
    assert response.json()["success"]
    assert run_once("queue-test-repaired",file_id)
    with db_session() as db:
        assert db.get(MarkdownFile,file_id).status == 2
        assert db.scalar(text("SELECT count(*) FROM t_index_job WHERE file_id=:id"), {"id":file_id}) == 2


def test_deadline_cannot_be_extended(queued_file):
    file_id,job_id = queued_file
    job = claim_job("queue-test-timeout",file_id)
    with db_session() as db:
        db.execute(text("UPDATE t_index_job SET deadline_at=clock_timestamp()-interval '1 second' WHERE id=:id"), {"id":job_id})
    assert not heartbeat("queue-test-timeout",job)
    with pytest.raises(LeaseLost):
        with db_session() as db:
            fence(db,job)
    recover_expired()
    assert job_row(job_id)["status"] == "queued"


def test_upload_only_enqueues_and_refuses_duplicate_reindex(admin_client,tmp_path,monkeypatch):
    from dataclasses import replace
    from app import main
    monkeypatch.setattr(main,"settings",replace(settings,md_storage_path=tmp_path))
    client,headers,_ = admin_client
    response = client.post("/customer-service/md/upload",files={"file":("queue.md",b"# Q\nA fact")},headers=headers)
    file_id = response.json()["data"]["id"]
    try:
        with db_session() as db:
            assert db.get(MarkdownFile,file_id).status == 0
            assert db.scalar(text("SELECT status FROM t_index_job WHERE file_id=:id"), {"id":file_id}) == "queued"
        result = client.post("/customer-service/md/reindex",json={"id":file_id},headers=headers)
        assert not result.json()["success"]
        rows = client.post("/customer-service/md/list",json={},headers=headers).json()["data"]
        assert next(r for r in rows if r["id"] == file_id)["task"]["status"] == "queued"
        assert not client.post("/customer-service/md/delete",json={"id":file_id},headers=headers).json()["success"]
    finally:
        with db_session() as db:
            db.execute(text("DELETE FROM t_admin_audit WHERE file_id=:id"), {"id":file_id})
            db.execute(text("DELETE FROM t_ai_customer_service_md_storage WHERE id=:id"), {"id":file_id})


def test_cross_process_worker_claim_survives_process_exit(queued_file):
    file_id,job_id = queued_file
    code = ("import os; from dataclasses import replace; import app.config as c; "
            "c.settings=replace(c.settings,database_name=" + repr(settings.database_name) + "); "
            "from app.task_queue import claim_job; assert claim_job('queue-test-child', " + str(file_id) + "); os._exit(0)")
    subprocess.run([sys.executable,"-c",code],cwd=PROJECT_ROOT,check=True,timeout=30,
                   capture_output=True,text=True)
    assert job_row(job_id)["status"] == "running"
    expire(job_id); recover_expired(); make_due(job_id)
    assert run_once("queue-test-rescuer",file_id)
    assert job_row(job_id)["status"] == "succeeded"
