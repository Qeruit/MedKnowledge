"""Real HTTP + real planner acceptance against one existing UNPUBLISHED file.

Creates a precheck task and audit history, never confirms submission or publishes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

import httpx
from sqlalchemy import text
from app.config import PROJECT_ROOT
from app.db import db_session


def snapshot(file_id):
    with db_session() as db:
        row = db.execute(text("SELECT row_to_json(f) FROM t_ai_customer_service_md_storage f WHERE id=:id"), {"id": file_id}).scalar_one()
        if row["is_published"]:
            raise RuntimeError("Only an unpublished test candidate may be selected")
        vectors = db.execute(text("SELECT id,content,metadata FROM t_vector_store WHERE metadata->>'mdStorageId'=:id ORDER BY id"), {"id": str(file_id)}).mappings().all()
        pages = db.execute(text("SELECT row_to_json(p) FROM t_pdf_page_audit p WHERE file_id=:id ORDER BY page_number"), {"id": file_id}).scalars().all()
        grants = db.execute(text("SELECT user_id FROM t_file_grant WHERE file_id=:id ORDER BY user_id"), {"id": file_id}).scalars().all()
    return hashlib.sha256(json.dumps([row, [dict(v) for v in vectors], pages, grants], sort_keys=True, default=str).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file-id", type=int, default=7)
    parser.add_argument("--base-url", default="http://web")
    parser.add_argument("--password-file", type=Path, default=Path("/app/.credentials/admin-bootstrap.json"))
    args = parser.parse_args()
    before = snapshot(args.file_id)
    credential = json.loads(args.password_file.read_text(encoding="utf-8"))
    with httpx.Client(base_url=args.base_url, timeout=30, headers={"Origin": "http://127.0.0.1:8088"}) as client:
        def post(path, body):
            response = client.post("/api" + path, json=body)
            response.raise_for_status()
            return response.json()["data"]
        login = post("/auth/login", {"username": credential["username"], "password": credential["password"]})
        client.headers["X-CSRF-Token"] = login["csrfToken"]
        try:
            task = post("/customer-service/precheck/create", {"id": args.file_id,
                "goal": "检查这份新资料是否具备送审条件，汇总真实阻塞项；不要批准或发布", "allowEvaluation": False})
            task_id = task["taskId"]
            deadline = time.monotonic() + 600
            while task["status"] in ("queued", "running", "waiting") and time.monotonic() < deadline:
                time.sleep(2)
                task = post("/customer-service/precheck/detail", {"taskId": task_id})
            checks = {"documentUnchanged": snapshot(args.file_id) == before,
                      "realPlannerExecuted": task["steps"] > 0 and bool(task["history"]),
                      "riskNotAutoApproved": task["status"] == "blocked" and bool(task["report"]["blockers"]),
                      "noEvaluationEnqueued": not any(event.get("tool") == "start_evaluation" for event in task["history"])}
            output = {"checks": checks, "task": task,
                      "scope": "real HTTP login/CSRF, native planner calls, real read-only tools; no submission/approval/publication"}
            destination = PROJECT_ROOT / "reports/demo/precheck-agent-live.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"checks": checks, "taskId": task_id, "status": task["status"], "steps": task["steps"]}, ensure_ascii=False))
            if not all(checks.values()):
                raise SystemExit(1)
        finally:
            client.post("/api/auth/logout", json={})


if __name__ == "__main__":
    main()
