"""Synthetic UI/recovery fixtures. No medical approval or publication operations."""
import argparse
import json
from pathlib import Path
import time
from uuid import uuid4

import httpx
from sqlalchemy import text
from app.config import PROJECT_ROOT
from app.db import MarkdownFile, db_session

STATE = PROJECT_ROOT / "reports/demo/agent-browser-fixture.json"
TEST_PASSWORD = "Synthetic-ui-only-20261020!"  # Disposable accounts; disabled after acceptance.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "new-draft", "recovery", "probe", "browser-proof", "cleanup"))
    parser.add_argument("--task-id")
    parser.add_argument("--stage", default="observed")
    parser.add_argument("--file-id", type=int)
    args = parser.parse_args()
    if args.action == "probe":
        with db_session() as db:
            task = db.execute(text("SELECT id,file_id,status,steps,errors,claim_token,lease_until,update_time FROM t_precheck_task WHERE id=:id"), {"id": args.task_id}).mappings().one()
        print(json.dumps(dict(task), default=str))
        return
    if args.action == "browser-proof":
        fixture = json.loads(STATE.read_text(encoding="utf-8"))
        selected = args.file_id or fixture["fileId"]
        with db_session() as db:
            filename = db.scalar(text("SELECT original_file_name FROM t_ai_customer_service_md_storage WHERE id=:id"), {"id": selected})
            if not filename or not filename.startswith("NON_MEDICAL_"):
                raise RuntimeError("Synthetic proof requires a NON_MEDICAL fixture")
            file = db.execute(text("SELECT id,review_status,is_published,reviewer_id,reviewed_at FROM t_ai_customer_service_md_storage WHERE id=:id"), {"id": selected}).mappings().one()
            tasks = db.execute(text("SELECT id,actor_id,status,steps,errors,state->'checks' AS checks,state->'history' AS history FROM t_precheck_task WHERE file_id=:id ORDER BY create_time"), {"id": selected}).mappings().all()
            submissions = db.scalar(text("SELECT count(*) FROM t_admin_audit WHERE file_id=:id AND action='version_submit'"), {"id": selected})
            evaluations = db.execute(text("SELECT id,status,cursor,total FROM t_release_eval_job WHERE candidate_id=:id"), {"id": selected}).mappings().all()
        path = PROJECT_ROOT / "reports/demo/agent-browser-boundary.json"
        output = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"stages": {}}
        stage = {"file": dict(file), "submissions": submissions, "tasks": [dict(t) for t in tasks], "evaluations": [dict(e) for e in evaluations]}
        output["stages"][args.stage] = stage
        path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps({"stage": args.stage, "file": dict(file), "submissions": submissions, "taskStates": [t["status"] for t in tasks]}, default=str))
        return
    credential = json.loads(Path("/app/.credentials/admin-bootstrap.json").read_text(encoding="utf-8"))
    with httpx.Client(base_url="http://web", timeout=40, headers={"Origin": "http://127.0.0.1:8088"}) as admin:
        def post(client, path, body):
            response = client.post("/api" + path, json=body)
            response.raise_for_status()
            return response.json()["data"]
        def login(client, username, password):
            info = post(client, "/auth/login", {"username": username, "password": password})
            client.headers["X-CSRF-Token"] = info["csrfToken"]
            return info
        login(admin, credential["username"], credential["password"])
        try:
            if args.action == "cleanup":
                data = json.loads(STATE.read_text(encoding="utf-8"))
                actor_ids = [info["id"] for info in data["users"].values()]
                with db_session() as db:
                    tasks = db.execute(text("SELECT id FROM t_precheck_task WHERE actor_id=ANY(:ids) AND status IN ('queued','running','waiting','awaiting_confirmation')"), {"ids": actor_ids}).scalars().all()
                for task_id in tasks:
                    post(admin, "/customer-service/precheck/cancel", {"taskId": str(task_id)})
                recovery_path = PROJECT_ROOT / "reports/demo/agent-recovery-fixture.json"
                if recovery_path.exists():
                    recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
                    with db_session() as db:
                        file = db.get(MarkdownFile, recovery["fileId"])
                        safe = bool(file and file.original_file_name.startswith("NON_MEDICAL_RECOVERY_"))
                    if safe:
                        post(admin, "/customer-service/precheck/cancel", {"taskId": recovery["taskId"]})
                for info in data["users"].values():
                    post(admin, "/admin/users/update", {"id": info["id"], "role": info["role"], "isActive": False})
                print("Synthetic UI accounts disabled and sessions revoked; records retained for audit.")
                return
            def upload(client, name):
                content = "# 非医疗软件验收资料\n\n## 测试识别码\n本合成测试资料的任务识别码为 UITEST829。仅用于软件测试，不对应任何真实器械。\n"
                response = client.post("/api/customer-service/md/upload", files={"file": (name, content.encode(), "text/markdown")})
                response.raise_for_status()
                file_id = response.json()["data"]["id"]
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    with db_session() as db:
                        row = db.get(MarkdownFile, file_id)
                        status = row.status
                    if status == 2:
                        break
                    if status == 3: raise RuntimeError("Synthetic fixture indexing failed")
                    time.sleep(1)
                if status != 2: raise TimeoutError("Synthetic indexing timed out")
                version = post(client, "/customer-service/md/versions/register", {"id": file_id, "revisionLabel": "NON-MEDICAL browser/recovery fixture"})
                return file_id, version
            if args.action in ("prepare", "new-draft"):
                suffix = uuid4().hex[:6]
                previous = json.loads(STATE.read_text(encoding="utf-8")) if args.action == "new-draft" else None
                users = previous["users"] if previous else {}
                if not previous:
                    for role in ("admin", "editor", "viewer"):
                        info = post(admin, "/admin/users", {"username": "qa_agent_" + role + "_" + suffix,
                            "password": TEST_PASSWORD, "role": role})
                        users[role] = info
                with httpx.Client(base_url="http://web", timeout=40, headers={"Origin": "http://127.0.0.1:8088"}) as editor:
                    login(editor, users["editor"]["username"], TEST_PASSWORD)
                    file_id, version = upload(editor, "NON_MEDICAL_UI_" + suffix + ".md")
                    post(editor, "/auth/logout", {})
                post(admin, "/customer-service/md/quality/cases/add", {"documentKey": version["documentKey"],
                    "question": "本合成测试资料的任务识别码是什么？", "kind": "fact", "critical": True,
                    "evidenceTerms": ["UITEST829"], "answerTerms": ["UITEST829"]})
                data = {"users": users, "fileId": file_id, "documentKey": version["documentKey"],
                        "note": "NON-MEDICAL synthetic fixture; accounts disabled after acceptance"}
                if previous:
                    data["previousFileIds"] = previous.get("previousFileIds", []) + [previous["fileId"]]
                STATE.parent.mkdir(parents=True, exist_ok=True)
                STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(data, ensure_ascii=False))
            else:
                file_id, version = upload(admin, "NON_MEDICAL_RECOVERY_" + uuid4().hex[:8] + ".md")
                task = post(admin, "/customer-service/precheck/create", {"id": file_id,
                    "goal": "检查合成资料的处理、索引和解析质量，达到基础条件后请求送审确认；不得自动确认或批准发布", "allowEvaluation": False})
                output = {"fileId": file_id, "taskId": task["taskId"], "initialTask": task}
                (PROJECT_ROOT / "reports/demo/agent-recovery-fixture.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"fileId": file_id, "taskId": task["taskId"], "status": task["status"]}))
        finally:
            admin.post("/api/auth/logout", json={})


if __name__ == "__main__":
    main()
