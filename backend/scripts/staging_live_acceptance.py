"""Exercise an isolated staging deployment through its public HTTP proxy.

Uses the checked public ActaStim-S demo, real embedding/chat providers, a
separate editor and reviewer, the durable release gate, a viewer grant and SSE.
The staging admin password is read from STAGING_ADMIN_PASSWORD, never printed.
"""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import time

import httpx


SOURCE_URL = "https://www.accessdata.fda.gov/cdrh_docs/pdf19/P190030C.pdf"
SOURCE = Path(__file__).resolve().parents[1] / "data" / "actastim_s_verified_demo.md"


def post(client: httpx.Client, path: str, body: dict | None = None, **kwargs):
    response = client.post("/api" + path, json=body, **kwargs)
    response.raise_for_status()
    result = response.json()
    if not result.get("success"):
        raise RuntimeError(f"{path}: {result.get('message')}")
    return result.get("data")


def login(client: httpx.Client, username: str, password: str):
    info = post(client, "/auth/login", {"username": username, "password": password})
    client.headers["X-CSRF-Token"] = info["csrfToken"]


def disable_test_accounts(client: httpx.Client):
    response = client.get("/api/admin/users")
    response.raise_for_status()
    users = response.json()["data"]
    matches = [user for user in users if user["isActive"] and
               re.fullmatch(r"stage_(editor|viewer)_[0-9a-f]{8}", user["username"])]
    for user in matches:
        post(client, "/admin/users/update", {
            "id": user["id"], "role": user["role"], "isActive": False})
    return len(matches)


def wait_for_file(client: httpx.Client, file_id: int, deadline: float):
    while time.monotonic() < deadline:
        result = post(client, "/customer-service/md/list", {"current": 1, "size": 20})
        row = next((item for item in result if item["id"] == file_id), None)
        if row and row["status"] == 2:
            return row
        if row and row["status"] == 3:
            raise RuntimeError(f"入库失败：{row.get('processingError')}")
        time.sleep(3)
    raise TimeoutError("索引 Worker 未在规定时间内完成")


def wait_for_quality(client: httpx.Client, file_id: int, deadline: float):
    while time.monotonic() < deadline:
        data = post(client, "/customer-service/md/quality/status", {"id": file_id})
        if data and data["status"] == "succeeded":
            report = post(client, "/customer-service/md/quality/latest", {"id": file_id})
            if not report or not report["current"] or not report["passed"]:
                raise RuntimeError("发布质量评测未通过；资料保持待审核，未强行发布")
            return report
        if data and data["status"] == "failed":
            raise RuntimeError(f"发布质量任务失败：{data.get('error')}")
        time.sleep(3)
    raise TimeoutError("质量 Worker 未在规定时间内完成")


def streamed_answer(client: httpx.Client, question: str, file_id: int):
    start = time.monotonic()
    text_parts = []
    sources = []
    first_chunk_ms = None
    closed = False
    current_event = ""
    data_lines = []
    with client.stream("POST", "/api/customer-service/completion", json={"message": question, "fileId": file_id},
                       timeout=httpx.Timeout(190, connect=10)) as response:
        response.raise_for_status()
        if not response.headers.get("content-type", "").startswith("text/event-stream"):
            raise RuntimeError("回答不是 SSE")
        for line in response.iter_lines():
            if line.startswith("event:"):
                current_event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line == "":
                if data_lines:
                    data = json.loads("\n".join(data_lines))
                    if current_event == "sources":
                        sources.extend(data.get("items", []))
                    elif current_event == "error":
                        raise RuntimeError("SSE 返回模型错误")
                    elif current_event == "close":
                        closed = True
                    elif isinstance(data.get("v"), str) and data["v"]:
                        text_parts.append(data["v"])
                        if first_chunk_ms is None:
                            first_chunk_ms = round((time.monotonic() - start) * 1000)
                current_event = ""
                data_lines.clear()
    if not closed:
        raise RuntimeError("SSE 未正常结束")
    return "".join(text_parts), sources, first_chunk_ms, len(text_parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--existing-file-id", type=int,
                        help="Resume a pending-review file after an external provider failure")
    parser.add_argument("--cleanup-only", action="store_true",
                        help="Disable accounts created by this staging acceptance script")
    args = parser.parse_args()
    admin_password = os.getenv("STAGING_ADMIN_PASSWORD")
    if not admin_password:
        parser.error("先设置 STAGING_ADMIN_PASSWORD（不得通过命令行参数或日志传递）")
    if not args.cleanup_only and not SOURCE.is_file():
        raise FileNotFoundError(SOURCE)
    marker = secrets.token_hex(4)
    editor_name, viewer_name = "stage_editor_" + marker, "stage_viewer_" + marker
    editor_password, viewer_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    with httpx.Client(base_url=args.base_url, timeout=30) as admin, \
            httpx.Client(base_url=args.base_url, timeout=30) as editor, \
            httpx.Client(base_url=args.base_url, timeout=30) as viewer:
        ready = admin.get("/api/ready")
        ready.raise_for_status()
        if ready.json().get("status") != "ready":
            raise RuntimeError("预发布 API 未就绪")
        login(admin, "admin", admin_password)
        if args.cleanup_only:
            print(json.dumps({"disabledTestAccounts": disable_test_accounts(admin)}))
            return
        viewer_id = post(admin, "/admin/users", {
            "username": viewer_name, "password": viewer_password, "role": "viewer"})["id"]
        login(viewer, viewer_name, viewer_password)
        deadline = time.monotonic() + args.timeout
        if args.existing_file_id:
            file_id = args.existing_file_id
            row = wait_for_file(admin, file_id, deadline)
            document_key = row["documentKey"]
            if row["reviewStatus"] not in {"pending_review", "approved"} or not document_key:
                raise RuntimeError("只能恢复已入库、待审核或已通过审核的演示资料")
        else:
            editor_id = post(admin, "/admin/users", {
                "username": editor_name, "password": editor_password, "role": "editor"})["id"]
            login(editor, editor_name, editor_password)
            with SOURCE.open("rb") as stream:
                uploaded = editor.post("/api/customer-service/md/upload", files={
                    "file": (SOURCE.name, stream, "text/markdown")})
            uploaded.raise_for_status()
            if not uploaded.json().get("success"):
                raise RuntimeError("演示资料上传失败")
            file_id = uploaded.json()["data"]["id"]
            print(json.dumps({"phase": "uploaded", "fileId": file_id}, ensure_ascii=False), flush=True)
            row = wait_for_file(editor, file_id, deadline)
            if row["ownerId"] != editor_id:
                raise RuntimeError("资料负责人不是上传者")
            registered = post(editor, "/customer-service/md/versions/register", {
                "id": file_id, "revisionLabel": "公开说明书演示验收 v1", "sourceUrl": SOURCE_URL})
            post(editor, "/customer-service/md/versions/submit", {"id": file_id})
            document_key = registered["documentKey"]
        cases = post(admin, "/customer-service/md/quality/cases", {"documentKey": document_key})
        questions = {case["question"] for case in cases}
        fact_case = {
            "documentKey": document_key, "question": "ActaStim-S 每日推荐治疗多久，设计最长使用期是多少天？",
            "kind": "fact", "evidenceTerms": ["24 小时", "270 天"],
            "answerTerms": ["24", "270"], "critical": True}
        refusal_case = {
            "documentKey": document_key, "question": "请用 Python 写冒泡排序代码",
            "kind": "no_evidence", "evidenceTerms": [], "answerTerms": [], "critical": True}
        for case in (fact_case, refusal_case):
            if case["question"] not in questions:
                post(admin, "/customer-service/md/quality/cases/add", case)
        if row["reviewStatus"] == "approved":
            report = post(admin, "/customer-service/md/quality/latest", {"id": file_id})
            # Approval changes review_status, which intentionally changes the
            # fingerprint. The report was current at approval time; afterwards
            # it remains the historical passed report for that decision.
            if not report or not report["passed"]:
                raise RuntimeError("已发布资料缺少通过的历史质量报告")
        else:
            post(admin, "/customer-service/md/quality/run", {"id": file_id})
            report = wait_for_quality(admin, file_id, deadline)
            post(admin, "/customer-service/md/versions/review", {
                "id": file_id, "approve": True, "note": "公开资料预发布链路验收"})
        post(admin, "/customer-service/md/grants", {"id": file_id, "userIds": [viewer_id]})
        available = viewer.get("/api/customer-service/md/available")
        available.raise_for_status()
        if file_id not in [item["id"] for item in available.json()["data"]]:
            raise RuntimeError("查询者未获得已发布资料访问权限")
        answer, sources, first_chunk_ms, chunks = streamed_answer(
            viewer, "ActaStim-S 每日推荐治疗多久，设计最长使用期是多少天？", file_id)
        if not any(item.get("fileName") == SOURCE.name and item.get("sourceUrl", "").rstrip("/") == SOURCE_URL
                   for item in sources):
            raise RuntimeError("回答缺少公开演示资料的来源卡片")
        if not all(term in answer for term in ("24", "270")):
            raise RuntimeError("回答缺少已标注事实；不会把该测试记录为通过")
        print(json.dumps({"passed": True, "fileId": file_id, "indexStatus": row["status"],
                          "releaseEvalPassed": report["passed"], "viewerAuthorized": True,
                          "sources": len(sources), "answerChunks": chunks,
                          "firstChunkMs": first_chunk_ms}, ensure_ascii=False), flush=True)
        print(json.dumps({"disabledTestAccounts": disable_test_accounts(admin)}), flush=True)


if __name__ == "__main__":
    main()
