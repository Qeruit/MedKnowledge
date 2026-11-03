"""Export an allowlisted, DB-enforced read-only handoff snapshot. No secrets."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT, settings
from app.db import MarkdownFile, User
from app.precheck_agent import public_task


def main():
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        with Session(bind=conn) as db:
            files = db.execute(text("""SELECT id,original_file_name,version_number,review_status,
                is_published,owner_id,reviewer_id,pdf_qa_revision FROM t_ai_customer_service_md_storage
                WHERE id IN (1,2,7,8,13) ORDER BY id""")).mappings().all()
            tasks = db.execute(text("SELECT * FROM t_precheck_task WHERE file_id IN (2,8,13) "
                "ORDER BY create_time DESC LIMIT 8")).mappings().all()
            task_views = []
            for row in tasks:
                task = public_task(db, dict(row), db.get(User, row["actor_id"]))
                task_views.append({key: task[key] for key in (
                    "taskId","fileId","version","status","current","steps","errors","allowEvaluation","checks","report")}
                    | {"tools": [item.get("tool") for item in task["history"]]})
            evaluations = db.execute(text("""SELECT candidate_id,id,passed,
                report->>'total' AS total,report->>'failed' AS failed,
                report->>'regressions' AS regressions,create_time
                FROM t_release_eval_run WHERE candidate_id IN (7,8,13)
                ORDER BY create_time DESC LIMIT 6""")).mappings().all()
            audits = db.execute(text("""SELECT action,file_id,actor_name,create_time
                FROM t_admin_audit WHERE file_id IN (7,13) AND action IN
                ('version_approve','version_self_approve_local_demo','precheck_confirm_submit')
                ORDER BY create_time DESC LIMIT 10""")).mappings().all()
            page_counts = db.execute(text("SELECT status,count(*) AS count FROM t_pdf_page_audit "
                "WHERE file_id=7 GROUP BY status ORDER BY status")).mappings().all()
            # Information_schema/metadata queries only; no model calls or writes.
        conn.rollback()
    engine.dispose()
    source_names = ("app/precheck_agent.py","app/versioning.py","app/knowledge.py",
                    "app/task_queue.py","app/quality_queue.py")
    hashes = {name: hashlib.sha256((PROJECT_ROOT/name).read_bytes()).hexdigest() for name in source_names}
    result = {"exportedAt":datetime.now(timezone.utc).isoformat(),
        "scope":"DB-enforced read-only snapshot; allowlisted fields only; no passwords, keys, sessions or document bodies",
        "documents":[dict(row) for row in files], "tasks":task_views,
        "evaluations":[dict(row) for row in evaluations],"approvalEvents":[dict(row) for row in audits],
        "file7PageStatuses":[dict(row) for row in page_counts],
        "localDemoSelfApproval":settings.local_demo_allow_self_approval,"backendSourceSha256":hashes}
    target=PROJECT_ROOT/"reports/demo/delivery_snapshot_20261020.json"
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    print(json.dumps({"snapshot":str(target),"documents":len(files),"tasks":len(tasks),
                      "localDemoSelfApproval":result["localDemoSelfApproval"]}))


if __name__=="__main__":
    main()
