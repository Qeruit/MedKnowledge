"""Run a small, real-model acceptance check of the asynchronous release gate.

This registers the existing unpublished ActaStim-S demonstration file as a
candidate, adds source-checked cases, and starts a durable quality job. It
does not approve or publish the candidate.
"""

import argparse
import json
import time

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import MarkdownFile, db_session
from app.main import app
from scripts.auth_helpers import login_client


SOURCE_URL = "https://www.accessdata.fda.gov/cdrh_docs/pdf19/P190030C.pdf"
FILE_NAME = "actastim_s_verified_demo.md"
CASES = [
    {
        "question": "ActaStim-S 的存储温度范围是多少？",
        "kind": "fact",
        "evidenceTerms": ["-25°C", "70°C"],
        "answerTerms": ["-25", "70"],
        "critical": True,
    },
    {
        "question": "ActaStim-S 电池充满最多约需要多久？",
        "kind": "fact",
        "evidenceTerms": ["3 小时"],
        "answerTerms": ["3"],
        "critical": True,
    },
    {
        "question": "ActaStim-S 每日推荐治疗多久，设计最长使用期是多少天？",
        "kind": "fact",
        "evidenceTerms": ["24 小时", "270 天"],
        "answerTerms": ["24", "270"],
        "critical": True,
    },
    {
        "question": "请用 Python 写冒泡排序代码",
        "kind": "no_evidence",
        "evidenceTerms": [],
        "answerTerms": [],
        "critical": True,
    },
]


def post(client: TestClient, path: str, payload: dict):
    response = client.post(path, json=payload)
    if response.status_code != 200:
        raise RuntimeError(f"{path}: HTTP {response.status_code}, {response.text[:300]}")
    body = response.json()
    if not body.get("success"):
        raise RuntimeError(f"{path}: {body.get('message')}")
    return body.get("data")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    with db_session() as db:
        file = db.scalar(select(MarkdownFile).where(MarkdownFile.original_file_name == FILE_NAME))
        if not file or file.status != 2 or file.is_published:
            raise RuntimeError("未找到已处理完成且未发布的 ActaStim-S 演示资料")
        file_id = file.id
        document_key = str(file.document_key) if file.document_key else None
        state = file.review_status
    with TestClient(app) as client:
        login_client(client)
        if not document_key:
            data = post(client, "/customer-service/md/versions/register", {
                "id": file_id, "revisionLabel": "公开演示资料验收 v1", "sourceUrl": SOURCE_URL,
            })
            document_key = data["documentKey"]
            state = data["reviewStatus"]
        if state == "draft":
            post(client, "/customer-service/md/versions/submit", {"id": file_id})
        elif state != "pending_review":
            raise RuntimeError(f"候选资料状态不是可评测状态：{state}")
        existing = post(client, "/customer-service/md/quality/cases", {"documentKey": document_key})
        questions = {case["question"] for case in existing}
        for case in CASES:
            if case["question"] not in questions:
                post(client, "/customer-service/md/quality/cases/add", {
                    "documentKey": document_key, **case,
                })
        queued = post(client, "/customer-service/md/quality/run", {"id": file_id})
        print(json.dumps({"candidateId": file_id, "documentKey": document_key,
                          "job": queued}, ensure_ascii=False), flush=True)
        deadline = time.monotonic() + args.timeout
        last = None
        while time.monotonic() < deadline:
            status = post(client, "/customer-service/md/quality/status", {"id": file_id})
            progress = (status["status"], status["cursor"], status["total"], status["attempts"])
            if progress != last:
                print(json.dumps({"progress": status}, ensure_ascii=False), flush=True)
                last = progress
            if status["status"] in {"succeeded", "failed"}:
                break
            time.sleep(3)
        else:
            raise TimeoutError("评测任务未在时限内完成，任务保留在队列中")
        if status["status"] != "succeeded":
            raise RuntimeError(f"质量任务失败：{status['error']}")
        report = post(client, "/customer-service/md/quality/latest", {"id": file_id})
        print(json.dumps({"report": report}, ensure_ascii=False), flush=True)
        if not report["passed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
