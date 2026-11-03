"""PostgreSQL durable queue with short claims, leases and fenced finalization."""
from uuid import uuid4

from sqlalchemy import select, text

from app.config import settings
from app.db import MarkdownFile, db_session


class LeaseLost(RuntimeError):
    pass


def enqueue(db, file_id: int):
    # Every producer locks the file before looking for a live task.
    file = db.scalar(select(MarkdownFile).where(MarkdownFile.id == file_id).with_for_update())
    if file is None:
        raise ValueError("文件不存在")
    live = db.scalar(text("SELECT id FROM t_index_job WHERE file_id=:id AND status IN ('queued','running')"), {"id": file_id})
    if live:
        return live
    job_id = uuid4()
    db.execute(text("""INSERT INTO t_index_job(id,file_id,status,max_attempts)
                       VALUES(:job,:file,'queued',:max)"""),
               {"job": job_id, "file": file_id, "max": settings.task_max_attempts})
    file.status = 0
    file.processing_error = None
    db.execute(text("UPDATE t_ai_customer_service_md_storage SET update_time=clock_timestamp() WHERE id=:id"), {"id": file_id})
    return job_id


def backfill_pending():
    """Explicit migration step; never reset a healthy running task on API restart."""
    with db_session() as db:
        files = db.scalars(select(MarkdownFile).where(MarkdownFile.status.in_((0, 1)))
                           .order_by(MarkdownFile.id).with_for_update()).all()
        for file in files:
            enqueue(db, file.id)
        return len(files)


def recover_expired():
    with db_session() as db:
        rows = db.execute(text("""SELECT * FROM t_index_job WHERE status='running'
            AND (lease_until <= clock_timestamp() OR deadline_at <= clock_timestamp())
            ORDER BY lease_until LIMIT 100 FOR UPDATE SKIP LOCKED""")).mappings().all()
        for row in rows:
            terminal = row["attempts"] >= row["max_attempts"]
            error = "Worker 租约或执行时限已过期" + ("，重试次数已耗尽" if terminal else "，等待自动重试")
            db.execute(text("""UPDATE t_index_job SET status=:status, claim_token=NULL,
                lease_until=NULL, deadline_at=NULL, worker_id=NULL, last_error=:error,
                available_at=clock_timestamp() + :delay * interval '1 second', update_time=clock_timestamp()
                WHERE id=:id"""), {"id": row["id"], "status": "failed" if terminal else "queued",
                                    "error": error, "delay": retry_delay(row["attempts"])})
            db.execute(text("""UPDATE t_ai_customer_service_md_storage SET status=:status,
                processing_error=:error, update_time=clock_timestamp() WHERE id=:id"""),
                       {"id": row["file_id"], "status": 3 if terminal else 0, "error": error})
        return len(rows)


def retry_delay(attempts):
    return min(settings.task_retry_seconds * 2 ** max(0, attempts - 1), 300)


def claim_job(worker_id, file_id=None):
    with db_session() as db:
        row = db.execute(text("""SELECT * FROM t_index_job
            WHERE status='queued' AND available_at <= clock_timestamp()
              AND (CAST(:file AS bigint) IS NULL OR file_id=:file)
            ORDER BY available_at,create_time LIMIT 1 FOR UPDATE SKIP LOCKED"""),
                         {"file": file_id}).mappings().first()
        if not row:
            return None
        token = uuid4()
        claimed = db.execute(text("""UPDATE t_index_job SET status='running',attempts=attempts+1,
            claim_token=:token,worker_id=:worker,
            lease_until=clock_timestamp() + :lease * interval '1 second',
            deadline_at=clock_timestamp() + :timeout * interval '1 second',update_time=clock_timestamp()
            WHERE id=:id RETURNING *"""), {"token": token, "worker": worker_id,
                "lease": settings.task_lease_seconds, "timeout": settings.task_timeout_seconds,
                "id": row["id"]}).mappings().one()
        db.execute(text("""UPDATE t_ai_customer_service_md_storage SET status=1,
            processing_error=NULL, processing_warnings='[]', update_time=clock_timestamp() WHERE id=:id"""),
                   {"id": row["file_id"]})
        return dict(claimed)


def heartbeat(worker_id, job=None):
    with db_session() as db:
        db.execute(text("""INSERT INTO t_worker_heartbeat(worker_id,last_seen,started_at)
            VALUES(:worker,clock_timestamp(),clock_timestamp())
            ON CONFLICT(worker_id) DO UPDATE SET last_seen=clock_timestamp()"""), {"worker": worker_id})
        if job:
            updated = db.execute(text("""UPDATE t_index_job SET
                lease_until=LEAST(deadline_at,clock_timestamp() + :lease * interval '1 second'),
                update_time=clock_timestamp()
                WHERE id=:id AND claim_token=:token AND status='running'
                  AND lease_until > clock_timestamp() AND deadline_at > clock_timestamp()
                RETURNING id"""), {"id": job["id"], "token": job["claim_token"],
                                      "lease": settings.task_lease_seconds}).scalar_one_or_none()
            return updated is not None
        return True


def fence(db, job):
    # Hold the job row lock through the final vector transaction. An old token
    # cannot write after reclaim; an expired lease cannot resurrect itself.
    row = db.execute(text("""SELECT * FROM t_index_job WHERE id=:id AND claim_token=:token
        AND status='running' AND lease_until > clock_timestamp() AND deadline_at > clock_timestamp()
        FOR UPDATE"""), {"id": job["id"], "token": job["claim_token"]}).mappings().first()
    if not row:
        raise LeaseLost("任务租约已失效，拒绝写入")
    return row


def succeed(db, job):
    # Called in the SAME transaction as all vectors and file status.
    db.execute(text("""UPDATE t_index_job SET status='succeeded',last_error=NULL,
        lease_until=NULL,deadline_at=NULL,claim_token=NULL,update_time=clock_timestamp()
        WHERE id=:id AND claim_token=:token"""), {"id": job["id"], "token": job["claim_token"]})


def fail_job(job, error, retryable=True):
    try:
        with db_session() as db:
            row = fence(db, job)
            terminal = not retryable or row["attempts"] >= row["max_attempts"]
            db.execute(text("""UPDATE t_index_job SET status=:status,last_error=:error,
                lease_until=NULL,deadline_at=NULL,claim_token=NULL,worker_id=NULL,
                available_at=clock_timestamp() + :delay * interval '1 second',update_time=clock_timestamp()
                WHERE id=:id"""), {"id": job["id"], "status": "failed" if terminal else "queued",
                                      "error": error[:500], "delay": retry_delay(row["attempts"])})
            db.execute(text("""UPDATE t_ai_customer_service_md_storage SET status=:status,
                processing_error=:error,update_time=clock_timestamp() WHERE id=:id"""),
                       {"id": job["file_id"], "status": 3 if terminal else 0, "error": error[:500]})
    except LeaseLost:
        return False
    return True
