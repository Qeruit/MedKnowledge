"""Register source-checked smoke cases and run the durable release evaluation.

This script does not approve or publish a document. Use a separate human
reviewer account for that step after reading the complete evidence and answers.
"""

import json
import hashlib
import argparse

from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import select

from app.config import PROJECT_ROOT
from app.db import MarkdownFile, db_session
from app.main import app
from evaluation.public_manual_review_cases import CASES
from scripts.auth_helpers import login_client


SOURCE_DIR = PROJECT_ROOT / "data" / "public_manuals"


def post(client, path, payload):
    response = client.post(path, json=payload)
    response.raise_for_status()
    body = response.json()
    if not body.get("success"):
        raise RuntimeError(f"{path}: {body.get('message')}")
    return body.get("data")


def main(file_name_filter=None):
    manifest = {item["file"]: item for item in json.loads(
        (SOURCE_DIR / "device_manuals.json").read_text(encoding="utf-8"))}
    client = TestClient(app)
    login_client(client)
    try:
        for file_name, cases in CASES.items():
            if file_name_filter and file_name != file_name_filter:
                continue
            source = SOURCE_DIR / file_name
            if hashlib.sha256(source.read_bytes()).hexdigest() != manifest[file_name]["sha256"]:
                raise RuntimeError(f"原始 PDF 哈希不符：{file_name}")
            reader = PdfReader(source)
            with db_session() as db:
                file = db.scalar(select(MarkdownFile).where(MarkdownFile.original_file_name == file_name))
                if file is None or file.status != 2 or file.review_status != "pending_review":
                    raise RuntimeError(f"待审且已索引文件不存在：{file_name}")
                file_id, key = file.id, str(file.document_key)
            existing = post(client, "/customer-service/md/quality/cases", {"documentKey": key})
            by_question = {item["question"]: item for item in existing}
            for case in cases:
                if case["page"] is not None:
                    original_text = reader.pages[case["page"] - 1].extract_text() or ""
                    normalized = " ".join(original_text.split())
                    if case["source"] not in normalized:
                        raise RuntimeError(f"原始 PDF 第 {case['page']} 页缺少核对语句：{file_name}")
                payload = {"documentKey": key, "question": case["question"],
                           "kind": "fact" if case["page"] else "no_evidence",
                           "evidenceTerms": case["evidenceTerms"],
                           "answerTerms": case["answerTerms"], "critical": True}
                prior = by_question.get(case["question"])
                if prior and any(prior[field] != payload[field] for field in
                                 ("kind", "evidenceTerms", "answerTerms", "critical")):
                    post(client, "/customer-service/md/quality/cases/delete", {"id": prior["id"]})
                    prior = None
                if not prior:
                    post(client, "/customer-service/md/quality/cases/add", payload)
            job = post(client, "/customer-service/md/quality/run", {"id": file_id})
            print(json.dumps({"fileId": file_id, "name": file_name,
                              "jobId": job["jobId"], "cases": len(cases)}, ensure_ascii=False), flush=True)
        # Quality jobs are intentionally asynchronous; report only their IDs.
    finally:
        client.post("/auth/logout")
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", choices=list(CASES))
    main(parser.parse_args().file)
