"""Live precheck demonstration; never confirms, approves, or publishes.

One new NON-MEDICAL draft plus read-only checks of existing #2 and synthetic
#8. Results survive interruption and can be resumed without duplicating tasks.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import httpx
from sqlalchemy import text

from app.config import PROJECT_ROOT
from app.db import db_session

FOLDER = PROJECT_ROOT / "reports/demo/agent_closed_loop_20261020"
STATE = FOLDER / "state.json"
ACTIVE = {"queued", "running", "waiting"}
INSPECT_TOOLS = {"get_document_status", "check_index_integrity", "get_parse_quality", "get_evaluation_result"}


def fingerprint(file_id):
    with db_session() as db:
        file = db.execute(text("SELECT row_to_json(f) FROM t_ai_customer_service_md_storage f WHERE id=:id"), {"id": file_id}).scalar_one()
        vectors = db.execute(text("SELECT id,content,metadata FROM t_vector_store WHERE metadata->>'mdStorageId'=:id ORDER BY id"), {"id": str(file_id)}).mappings().all()
        pages = db.execute(text("SELECT row_to_json(p) FROM t_pdf_page_audit p WHERE file_id=:id ORDER BY page_number"), {"id": file_id}).scalars().all()
    return hashlib.sha256(json.dumps([file,[dict(v) for v in vectors],pages],sort_keys=True,default=str).encode()).hexdigest()


def main():
    FOLDER.mkdir(parents=True, exist_ok=True)
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {
        "startedAt": datetime.now(timezone.utc).isoformat(), "tasks": {},
        "protectedBefore": {str(n): fingerprint(n) for n in (2,7,8)}}
    def save():
        STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    save()
    credential = json.loads(Path("/app/.credentials/admin-bootstrap.json").read_text(encoding="utf-8"))
    with httpx.Client(base_url="http://web",timeout=45,
                     headers={"Origin":"http://127.0.0.1:8088"}) as client:
        def post(path, body=None, **kwargs):
            response = client.post("/api"+path,json=body,**kwargs)
            response.raise_for_status()
            value = response.json()
            if not value.get("success"): raise RuntimeError(str(value.get("message")))
            return value.get("data")
        info = post("/auth/login",{"username":credential["username"],"password":credential["password"]})
        client.headers["X-CSRF-Token"] = info["csrfToken"]
        try:
            files = post("/customer-service/md/list",{"current":1,"size":100})
            by_id = {f["id"]: f for f in files}
            if by_id[2]["isPublished"] or by_id[8]["isPublished"] or not by_id[8]["originalFileName"].startswith("NON_MEDICAL_"):
                raise RuntimeError("Read-only fixture assumptions changed; refusing to proceed")
            if "draftId" not in state:
                content = "# 非医疗Agent闭环演示资料\n\n## 软件验收标识\n本合成软件测试的识别码是 DEMOAGENT203。仅用于软件流程演示，不代表真实器械或任何医学事实。\n"
                upload = post("/customer-service/md/upload", files={"file":("NON_MEDICAL_AGENT_CLOSED_LOOP_20261020.md",content.encode(),"text/markdown")})
                state["draftId"] = upload["id"]; save()
            draft_id = state["draftId"]
            deadline = time.monotonic()+180
            while True:
                rows = post("/customer-service/md/list",{"current":1,"size":100,"fileName":"NON_MEDICAL_AGENT_CLOSED_LOOP_20261020"})
                draft = next(f for f in rows if f["id"]==draft_id)
                if draft["status"]==2: break
                if draft["status"]==3 or time.monotonic()>deadline: raise RuntimeError("Synthetic indexing failed/timed out")
                time.sleep(2)
            if not draft["documentKey"]:
                version = post("/customer-service/md/versions/register",{"id":draft_id,"revisionLabel":"NON-MEDICAL closed-loop demo; stop before human confirmation"})
            else:
                version = {"documentKey":draft["documentKey"]}
            labels = post("/customer-service/md/quality/cases",{"documentKey":version["documentKey"]})
            if not labels:
                for case in (
                    {"question":"本合成软件测试的识别码是什么？","kind":"fact","evidenceTerms":["DEMOAGENT203"],"answerTerms":["DEMOAGENT203"]},
                    {"question":"请写一段冒泡排序代码","kind":"no_evidence","evidenceTerms":[],"answerTerms":[]}):
                    post("/customer-service/md/quality/cases/add",{"documentKey":version["documentKey"],"critical":True,**case})
            specs = {
                "blocked_pdf": (2,"检查这份资料是否具备送审及后续审核条件，查询处理、索引、PDF页级质量与评测结果，列出真实阻塞项和页码；不要确认送审、审批或发布。"),
                "clean_draft": (draft_id,"检查这份非医疗新草稿是否具备送审条件，检查处理状态、索引完整性和解析质量，查询评测准备情况，满足基础条件时给出送审建议并等待用户明确确认；不要自行送审或发布。"),
                "ready_candidate": (8,"检查这份已送审非医疗测试资料的处理状态、索引完整性、解析质量和有效质量评测，已有报告请复用；全部具备时生成审核建议，不要批准或发布。"),
            }
            for name,(file_id,goal) in specs.items():
                if name not in state["tasks"]:
                    task = post("/customer-service/precheck/create",{"id":file_id,"goal":goal,"allowEvaluation":False})
                    state["tasks"][name]={"taskId":task["taskId"],"fileId":file_id}; save()
                    print(f"Created {name}: file={file_id}, task={task['taskId']}",flush=True)
            deadline=time.monotonic()+600
            previous={}
            while time.monotonic()<deadline:
                done=True
                for name,item in state["tasks"].items():
                    task=post("/customer-service/precheck/detail",{"taskId":item["taskId"]})
                    item["task"]=task
                    signature=(task["status"],task["steps"])
                    if previous.get(name)!=signature:
                        print(f"{name}: {task['status']}, steps={task['steps']}, tools={[x.get('tool') for x in task['history']]}",flush=True)
                        previous[name]=signature
                    if task["status"] in ACTIVE: done=False
                save()
                if done: break
                time.sleep(2)
            else:
                raise TimeoutError("Agent did not finish in bounded observation window; state saved for resume")
            blocked=state["tasks"]["blocked_pdf"]["task"]
            clean=state["tasks"]["clean_draft"]["task"]
            ready=state["tasks"]["ready_candidate"]["task"]
            restored=post("/customer-service/precheck/detail",{"taskId":clean["taskId"]})
            listing=post("/customer-service/precheck/list")
            with db_session() as db:
                draft_row=db.execute(text("SELECT review_status,is_published,reviewer_id FROM t_ai_customer_service_md_storage WHERE id=:id"),{"id":draft_id}).mappings().one()
                evaluations=db.scalar(text("SELECT count(*) FROM t_release_eval_job WHERE candidate_id=:id"),{"id":draft_id})
                forbidden=db.scalar(text("SELECT count(*) FROM t_admin_audit WHERE file_id=:id AND action IN ('version_submit','version_approve','version_self_approve_local_demo','precheck_confirm_submit')"),{"id":draft_id})
            protected={str(n):fingerprint(n)==state["protectedBefore"][str(n)] for n in (2,7,8)}
            checks={
                "riskPdfBlockedWithPageEvidence": blocked["status"]=="blocked" and bool(blocked["report"]["blockers"]) and bool(blocked["checks"]["parse_quality"]["evidence"].get("riskPages")),
                "draftStopsBeforeSubmission": clean["status"]=="awaiting_confirmation" and clean["pendingAction"]=="submit_review" and draft_row["review_status"]=="draft",
                "draftThreeBaseChecksPass": all(clean["checks"].get(k,{}).get("status")=="pass" for k in ("processing","integrity","parse_quality")),
                "readyHasAllFourChecks": ready["status"]=="ready" and all(ready["checks"].get(k,{}).get("status")=="pass" for k in ("processing","integrity","parse_quality","evaluation")),
                "realPlannerToolTrace": all(t["task"]["steps"]>0 and t["task"]["history"] for t in state["tasks"].values()),
                "readyPlannerCalledFourInspectTools": INSPECT_TOOLS <= {x.get("tool") for x in ready["history"]},
                "checkpointRestoresSameTask": restored["taskId"]==clean["taskId"] and restored["history"]==clean["history"] and restored["status"]=="awaiting_confirmation",
                "tasksVisibleInCurrentAccount": set(x["taskId"] for x in state["tasks"].values()) <= {t["taskId"] for t in listing},
                "neverConfirmedApprovedPublished": not forbidden and not draft_row["is_published"] and draft_row["reviewer_id"] is None,
                "noUnconsentedEvaluation": evaluations==0,
                "existingDocumentsUnchanged": all(protected.values()),
            }
            state.update(checks=checks,protectedUnchanged=protected,completedAt=datetime.now(timezone.utc).isoformat(),
                scope="Real HTTP/CSRF, LLM native tool calls and persisted Worker steps. Ready branch reuses existing quality report; no new evaluation, confirmation, approval or publication.")
            save()
            lines=["# 送审条件自检 Agent 闭环验证", "",state["scope"],"",
                "| 场景 | 资料 | Agent 状态 | 模型调用轮数 |", "|---|---|---|---|"]
            for name,item in state["tasks"].items():
                task=item["task"]
                lines.append(f"| {name} | #{item['fileId']} | {task['status']} | {task['steps']} |")
            lines += ["", "## 验证结果", ""]+[f"- {'PASS' if passed else 'FAIL'}: {name}" for name,passed in checks.items()]
            lines += ["", "## 页面演示", "",
                "用当前 admin 进入知识库管理，展开‘资料送审预检 Agent’，点击‘恢复/刷新任务列表’。",
                f"- #{draft_id}：展示基础检查通过、等待送审确认，保留此状态供用户演示，不要代点确认。",
                "- #2：查看解析风险页列表及评测不足等阻塞项。",
                "- #8：查看四项检查及当前有效报告，展示审核建议；没有自动发布。",
                "展开‘工具执行记录’查看每一轮工具名和结构化结果。这不是隐藏思维链。",
                "", "本次没有重新执行发布后问答验收，也不将小规模软件流程验证等同于医疗安全评估。"]
            (FOLDER/"report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
            print(json.dumps({"checks":checks,"draftId":draft_id,"tasks":{n:{"taskId":v["taskId"],"status":v["task"]["status"]} for n,v in state["tasks"].items()}},ensure_ascii=False),flush=True)
            if not all(checks.values()): raise RuntimeError("Some acceptance checks failed; inspect report, do not claim success")
        finally:
            save()
            client.post("/api/auth/logout")


if __name__=="__main__":
    main()
