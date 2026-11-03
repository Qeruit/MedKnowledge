"""Durable, fenced release evaluation queue with per-case checkpoints."""

import asyncio
from datetime import datetime
import json
from uuid import uuid4

from sqlalchemy import select, text

from app.audit import audit
from app.config import settings
from app.db import MarkdownFile, ReleaseEvalJob, ReleaseEvalRun, User, db_session
from app.task_queue import LeaseLost, retry_delay


QUALITY_TIMEOUT_SECONDS = max(3600, settings.task_timeout_seconds)


class EvaluationStale(ValueError):
    pass


def enqueue_quality(db, candidate_id: int, baseline_id: int | None, actor_id: int,
                    fingerprint: str, total: int):
    # Serialize submissions for one candidate and release the partial unique index
    # before inserting a job for a new fingerprint.
    db.scalar(select(MarkdownFile.id).where(MarkdownFile.id == candidate_id).with_for_update())
    live = db.scalar(select(ReleaseEvalJob).where(ReleaseEvalJob.candidate_id == candidate_id,
                                                  ReleaseEvalJob.status.in_(("queued", "running"))))
    if live and live.fingerprint == fingerprint:
        return live.id
    if live:
        live.status = "failed"
        live.last_error = "评测期间资料、题库或配置已变化，请运行新任务"
        live.claim_token = None
        live.lease_until = None
        live.deadline_at = None
        db.flush()
    job_id = uuid4()
    db.execute(text("""INSERT INTO t_release_eval_job
        (id,candidate_id,baseline_id,actor_id,fingerprint,status,cursor,total,
         results,attempts,max_attempts,available_at,create_time,update_time)
        VALUES (:id,:candidate,:baseline,:actor,:fingerprint,'queued',0,:total,
                '[]',0,:max,clock_timestamp(),clock_timestamp(),clock_timestamp())"""), {
        "id": job_id, "candidate": candidate_id, "baseline": baseline_id,
        "actor": actor_id, "fingerprint": fingerprint, "total": total,
        "max": settings.task_max_attempts,
    })
    return job_id


def recover_expired_quality():
    with db_session() as db:
        rows = db.execute(text("""SELECT * FROM t_release_eval_job WHERE status='running'
            AND (lease_until<=clock_timestamp() OR deadline_at<=clock_timestamp())
            ORDER BY lease_until LIMIT 100 FOR UPDATE SKIP LOCKED""")).mappings().all()
        for row in rows:
            terminal = row["attempts"] >= row["max_attempts"]
            db.execute(text("""UPDATE t_release_eval_job SET status=:status,claim_token=NULL,
                worker_id=NULL,lease_until=NULL,deadline_at=NULL,
                last_error=:error,available_at=clock_timestamp()+:delay*interval '1 second',
                update_time=clock_timestamp() WHERE id=:id"""), {
                "id": row["id"], "status": "failed" if terminal else "queued",
                "error": "Worker 租约或执行时限过期" + ("，次数耗尽" if terminal else "，等待重试"),
                "delay": retry_delay(row["attempts"]),
            })
        return len(rows)


def claim_quality(worker_id: str, candidate_id: int | None = None):
    with db_session() as db:
        row = db.execute(text("""SELECT * FROM t_release_eval_job
            WHERE status='queued' AND available_at<=clock_timestamp()
              AND (CAST(:candidate AS bigint) IS NULL OR candidate_id=:candidate)
            ORDER BY available_at,create_time LIMIT 1 FOR UPDATE SKIP LOCKED"""),
                         {"candidate": candidate_id}).mappings().first()
        if not row:
            return None
        token = uuid4()
        result = db.execute(text("""UPDATE t_release_eval_job SET
            status='running',attempts=attempts+1,claim_token=:token,worker_id=:worker,
            lease_until=clock_timestamp()+:lease*interval '1 second',
            deadline_at=clock_timestamp()+:timeout*interval '1 second',
            update_time=clock_timestamp() WHERE id=:id RETURNING *"""), {
            "id": row["id"], "token": token, "worker": worker_id,
            "lease": settings.task_lease_seconds, "timeout": QUALITY_TIMEOUT_SECONDS,
        }).mappings().one()
        return dict(result)


def renew_quality(worker_id: str, job: dict):
    with db_session() as db:
        db.execute(text("""INSERT INTO t_worker_heartbeat(worker_id,last_seen,started_at)
            VALUES(:worker,clock_timestamp(),clock_timestamp())
            ON CONFLICT(worker_id) DO UPDATE SET last_seen=clock_timestamp()"""), {"worker": worker_id})
        updated = db.execute(text("""UPDATE t_release_eval_job SET
            lease_until=LEAST(deadline_at,clock_timestamp()+:lease*interval '1 second'),
            update_time=clock_timestamp()
            WHERE id=:id AND claim_token=:token AND status='running'
              AND lease_until>clock_timestamp() AND deadline_at>clock_timestamp()
            RETURNING id"""), {"id": job["id"], "token": job["claim_token"],
                             "lease": settings.task_lease_seconds}).scalar_one_or_none()
        return updated is not None


def _fence(db, job: dict):
    row = db.execute(text("""SELECT * FROM t_release_eval_job WHERE id=:id AND claim_token=:token
        AND status='running' AND lease_until>clock_timestamp() AND deadline_at>clock_timestamp()
        FOR UPDATE"""), {"id": job["id"], "token": job["claim_token"]}).mappings().first()
    if not row:
        raise LeaseLost("评测租约已失效，拒绝写入")
    return row


def fail_quality(job: dict, error: str, *, retryable: bool):
    try:
        with db_session() as db:
            row = _fence(db, job)
            terminal = not retryable or row["attempts"] >= row["max_attempts"]
            db.execute(text("""UPDATE t_release_eval_job SET
                status=:status,last_error=:error,claim_token=NULL,worker_id=NULL,
                lease_until=NULL,deadline_at=NULL,
                available_at=clock_timestamp()+:delay*interval '1 second',
                update_time=clock_timestamp() WHERE id=:id"""), {
                "id": job["id"], "status": "failed" if terminal else "queued",
                "error": error[:500], "delay": retry_delay(row["attempts"]),
            })
    except LeaseLost:
        return False
    return True


def _validated_snapshot(db, job: dict):
    from app.release_quality import _cases, _fingerprint

    candidate = db.get(MarkdownFile, job["candidate_id"])
    if not candidate or candidate.status != 2 or candidate.review_status != "pending_review":
        raise EvaluationStale("候选版本已不处于待审核状态")
    baseline = db.scalar(select(MarkdownFile).where(MarkdownFile.document_key == candidate.document_key,
                                                    MarkdownFile.is_published.is_(True)))
    cases = _cases(db, candidate.document_key)
    if (not cases or (baseline.id if baseline else None) != job["baseline_id"]
            or _fingerprint(candidate, baseline, cases) != job["fingerprint"]):
        raise EvaluationStale("资料、基线、模型配置或标注题已变化，请重新评测")
    actor = db.get(User, job["actor_id"])
    if not actor or not actor.is_active or actor.role != "admin":
        raise EvaluationStale("发起评测的管理员账号已失效")
    return cases, actor


def _one_case(job: dict, case, actor):
    from app.release_quality import _case_data, _evaluate_side

    async def evaluate():
        baseline = await _evaluate_side(case, job["baseline_id"], actor) if job["baseline_id"] else None
        candidate = await _evaluate_side(case, job["candidate_id"], actor)
        return baseline, candidate

    baseline, candidate = asyncio.run(evaluate())
    return {"case": _case_data(case), "baseline": baseline, "candidate": candidate,
            "regression": bool(baseline and baseline["passed"] and not candidate["passed"])}


def process_quality(job: dict):
    while True:
        with db_session() as db:
            row = _fence(db, job)
            cases, actor = _validated_snapshot(db, job)
            cursor = row["cursor"]
            if cursor >= len(cases):
                break
            case = cases[cursor]
        result = _one_case(job, case, actor)
        with db_session() as db:
            row = _fence(db, job)
            cases, _ = _validated_snapshot(db, job)
            if row["cursor"] != cursor or cases[cursor].id != case.id:
                raise LeaseLost("评测进度已由其他 Worker 更新")
            results = list(row["results"])
            results.append(result)
            db.execute(text("""UPDATE t_release_eval_job SET cursor=:cursor,results=CAST(:results AS json),
                last_error=NULL,update_time=clock_timestamp() WHERE id=:id"""), {
                "id": job["id"], "cursor": cursor + 1,
                "results": json.dumps(results, ensure_ascii=False, default=str),
            })

    with db_session() as db:
        row = _fence(db, job)
        cases, actor = _validated_snapshot(db, job)
        if row["cursor"] != len(cases) or len(row["results"]) != len(cases):
            raise LeaseLost("评测题数与检查点不一致")
        results = row["results"]
        failures = [item for item in results if not item["candidate"]["passed"]]
        regressions = [item for item in results if item["regression"]]
        passed = not failures and not regressions
        report = {"passed": passed, "total": len(results), "failed": len(failures),
                  "regressions": len(regressions),
                  "criticalFailed": sum(item["case"]["critical"] for item in failures),
                  "baselineId": job["baseline_id"], "candidateId": job["candidate_id"],
                  "cases": results}
        run_id = uuid4()
        db.add(ReleaseEvalRun(id=run_id, candidate_id=job["candidate_id"],
                              baseline_id=job["baseline_id"], fingerprint=job["fingerprint"],
                              passed=passed, report=report, actor_id=actor.id, create_time=datetime.now()))
        db.execute(text("""UPDATE t_release_eval_job SET status='succeeded',report_id=:report,
            claim_token=NULL,worker_id=NULL,lease_until=NULL,deadline_at=NULL,
            last_error=NULL,update_time=clock_timestamp() WHERE id=:id"""),
                   {"id": job["id"], "report": run_id})
        audit(db, "quality_eval", job["candidate_id"],
              f"job={job['id']}; run={run_id}; passed={passed}; cases={len(cases)}", actor=actor)
        return run_id
