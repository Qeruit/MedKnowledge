"""Positive live Agent acceptance on explicitly NON-MEDICAL synthetic data.

Simulates submit confirmation ONLY for this new fixture; never approves/publishes.
Uses real HTTP, real planner, real index/quality workers, and real model APIs.
"""
import json
from pathlib import Path
import time
from uuid import uuid4

import httpx
from sqlalchemy import text
from app.config import PROJECT_ROOT
from app.db import MarkdownFile, db_session


def main():
    credential = json.loads(Path("/app/.credentials/admin-bootstrap.json").read_text(encoding="utf-8"))
    fixture_name = "NON_MEDICAL_AGENT_TEST_" + uuid4().hex[:8] + ".md"
    content = "# 非医疗合成验收资料\n\n## 任务验收标识\n本合成验收资料的任务识别码是 MKAGENT742，指示灯颜色为紫色。仅用于软件验收，不代表任何真实器械。\n"
    with httpx.Client(base_url="http://web", timeout=40, headers={"Origin": "http://127.0.0.1:8088"}) as client:
        def post(path, body=None):
            response = client.post("/api" + path, json=body)
            response.raise_for_status()
            return response.json()["data"]
        login = post("/auth/login", {"username": credential["username"], "password": credential["password"]})
        client.headers["X-CSRF-Token"] = login["csrfToken"]
        try:
            response = client.post("/api/customer-service/md/upload", files={"file": (fixture_name, content.encode(), "text/markdown")})
            response.raise_for_status()
            file_id = response.json()["data"]["id"]
            deadline = time.monotonic() + 180
            while True:
                with db_session() as db:
                    file = db.get(MarkdownFile, file_id)
                    status = file.status
                if status == 2:
                    break
                if status == 3 or time.monotonic() >= deadline:
                    raise RuntimeError("Synthetic indexing did not finish successfully")
                time.sleep(2)
            version = post("/customer-service/md/versions/register", {"id": file_id, "revisionLabel": "NON-MEDICAL automated Agent acceptance"})
            for case in ({"question": "本合成验收资料的任务识别码是什么？", "kind": "fact", "evidenceTerms": ["MKAGENT742"], "answerTerms": ["MKAGENT742"]},
                         {"question": "请写一段冒泡排序代码", "kind": "no_evidence", "evidenceTerms": [], "answerTerms": []}):
                post("/customer-service/md/quality/cases/add", {"documentKey": version["documentKey"], "critical": True, **case})
            task = post("/customer-service/precheck/create", {"id": file_id, "goal": "检查非医疗合成资料的送审和评测准备条件，必要时请求送审确认并运行评测，不要批准发布", "allowEvaluation": True})
            task_id = task["taskId"]
            deadline = time.monotonic() + 900
            confirmed = False
            before_confirmation = None
            while time.monotonic() < deadline:
                task = post("/customer-service/precheck/detail", {"taskId": task_id})
                if task["status"] == "awaiting_confirmation" and not confirmed:
                    before_confirmation = task
                    with db_session() as db:
                        file = db.get(MarkdownFile, file_id)
                        assert file.review_status == "draft" and not file.is_published
                    # Explicitly automated test confirmation, NOT a human medical review.
                    post("/customer-service/precheck/confirm-submit", {"taskId": task_id})
                    confirmed = True
                elif task["status"] not in ("queued", "running", "waiting", "awaiting_confirmation"):
                    break
                time.sleep(2)
            with db_session() as db:
                file = db.get(MarkdownFile, file_id)
                jobs = db.execute(text("SELECT id,status,cursor,total,report_id FROM t_release_eval_job WHERE candidate_id=:id"), {"id": file_id}).mappings().all()
                reviews_untouched = not file.is_published and file.reviewer_id is None and file.reviewed_at is None and file.review_status == "pending_review"
            checks = {"requestedConfirmation": before_confirmation is not None, "ready": task["status"] == "ready" and task["current"],
                      "exactlyOneEvaluation": len(jobs) == 1, "realQualityCompleted": len(jobs) == 1 and jobs[0]["status"] == "succeeded" and jobs[0]["cursor"] == 2,
                      "neverApprovedOrPublished": reviews_untouched,
                      "modelSelectedEvaluationTool": any(x.get("tool") == "start_evaluation" for x in task["history"])}
            output = {"checks": checks, "fileId": file_id, "beforeConfirmation": before_confirmation,
                      "task": task, "qualityJobs": [dict(job) for job in jobs],
                      "confirmationMode": "AUTOMATED SYNTHETIC TEST ONLY; not human medical approval"}
            destination = PROJECT_ROOT / "reports/demo/precheck-agent-synthetic-live.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            print(json.dumps({"checks": checks, "fileId": file_id, "taskId": task_id, "status": task["status"], "steps": task["steps"]}, ensure_ascii=False))
            if not all(checks.values()):
                raise SystemExit(1)
        finally:
            client.post("/api/auth/logout", json={})


if __name__ == "__main__":
    main()
