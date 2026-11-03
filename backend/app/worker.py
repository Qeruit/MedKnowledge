"""Run separately: python -m app.worker. No tasks execute in API processes."""
import argparse
import logging
import os
import signal
import socket
import threading
from uuid import uuid4

import httpx

from app.config import settings
from app.documents import DocumentReadError
from app.db import db_session
from sqlalchemy import text
from app.knowledge import index_markdown
from app.task_queue import LeaseLost, claim_job, fail_job, heartbeat, recover_expired
from app.quality_queue import (EvaluationStale, claim_quality, fail_quality, process_quality,
                               recover_expired_quality, renew_quality)
from app.storage_guard import storage_lock

logger = logging.getLogger(__name__)


def process_job(job, worker_id):
    finished = threading.Event()

    def renew():
        while not finished.wait(settings.task_heartbeat_seconds):
            try:
                if not heartbeat(worker_id, job):
                    logger.warning("Lease lost for job %s", job['id'])
                    return
            except Exception:
                # Do not pretend the lease is alive after a DB outage.
                logger.exception("Heartbeat failed for job %s", job['id'])

    thread = threading.Thread(target=renew, name="task-lease", daemon=True)
    thread.start()
    try:
        index_markdown(job["file_id"], job)
        logger.info("Completed job=%s file=%s attempt=%s", job['id'], job['file_id'], job['attempts'])
    except LeaseLost:
        logger.warning("Discarded expired result for job=%s", job['id'])
    except Exception as exc:
        logger.exception("Index job failed job=%s file=%s", job['id'], job['file_id'])
        permanent = isinstance(exc, (DocumentReadError, FileNotFoundError, ValueError))
        if isinstance(exc, httpx.HTTPStatusError):
            permanent = 400 <= exc.response.status_code < 500 and exc.response.status_code not in (408, 429)
        message = str(exc)[:500] if isinstance(exc, DocumentReadError) else (
            "解析或配置错误，请修复后手动重试" if permanent else "外部服务或数据库暂时失败，自动重试达到上限后需人工处理")
        fail_job(job, message, retryable=not permanent)
    finally:
        finished.set()
        thread.join(timeout=5)


def process_quality_job(job, worker_id):
    finished = threading.Event()

    def renew():
        while not finished.wait(settings.task_heartbeat_seconds):
            try:
                if not renew_quality(worker_id, job):
                    logger.warning("Quality lease lost job=%s", job["id"])
                    return
            except Exception:
                logger.exception("Quality lease heartbeat failed job=%s", job["id"])

    thread = threading.Thread(target=renew, name="quality-lease", daemon=True)
    thread.start()
    try:
        report_id = process_quality(job)
        logger.info("Completed quality job=%s report=%s", job["id"], report_id)
    except LeaseLost:
        logger.warning("Discarded expired quality result job=%s", job["id"])
    except Exception as exc:
        logger.exception("Quality job failed job=%s", job["id"])
        permanent = isinstance(exc, EvaluationStale)
        if isinstance(exc, httpx.HTTPStatusError):
            permanent = 400 <= exc.response.status_code < 500 and exc.response.status_code not in (408, 429)
        error = str(exc)[:500] if permanent else "模型或数据库暂时失败，等待自动重试；次数耗尽后可重新提交"
        fail_quality(job, error, retryable=not permanent)
    finally:
        finished.set()
        thread.join(timeout=5)


def run_once(worker_id="test-worker", file_id=None, queue_type="all"):
    heartbeat(worker_id)
    if queue_type in ("all", "index"):
        with storage_lock():
            recover_expired()
            job = claim_job(worker_id, file_id=file_id)
            if job:
                process_job(job, worker_id)
                return True
    if file_id is None and queue_type in ("all", "quality"):
        recover_expired_quality()
        job = claim_quality(worker_id)
        if job:
            process_quality_job(job, worker_id)
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process at most one task then exit")
    parser.add_argument("--queue", choices=("all", "index", "quality"), default="all",
                        help="Dedicated workers avoid long evaluations delaying file indexing")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    logger.info("Worker started id=%s; shared storage=%s", worker_id, settings.md_storage_path)
    try:
        while not stopping.is_set():
            try:
                processed = run_once(worker_id, queue_type=args.queue)
            except Exception:
                logger.exception("Worker poll failed; pending tasks remain in PostgreSQL")
                processed = False
            if args.once:
                break
            if not processed:
                stopping.wait(settings.task_poll_seconds)
    finally:
        try:
            with db_session() as db:
                db.execute(text("DELETE FROM t_worker_heartbeat WHERE worker_id=:id"), {"id": worker_id})
        except Exception:
            logger.warning("Could not remove heartbeat; it will age out")
        logger.info("Worker stopped id=%s", worker_id)


if __name__ == "__main__":
    main()
