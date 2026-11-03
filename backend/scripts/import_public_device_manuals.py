"""Stage checked manufacturer manuals through the authenticated upload API.

Indexing remains in the durable Worker. New versions are submitted for human
review; this command never bypasses the release quality gate or publishes them.
"""

import hashlib
import json
from pathlib import Path
import time

from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import select, text

from app.config import PROJECT_ROOT, settings
from app.db import MarkdownFile, db_session
from app.documents import read_documents
from app.main import app
from scripts.auth_helpers import login_client


MANUALS_DIR = PROJECT_ROOT / "data" / "public_manuals"


def _checked_source(info):
    source = MANUALS_DIR / info["file"]
    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != info["sha256"]:
        raise RuntimeError(f"原文件不存在或 SHA-256 不匹配：{info['file']}")
    if len(PdfReader(source).pages) != info["pdfPages"]:
        raise RuntimeError(f"PDF 页数与来源清单不一致：{info['file']}")
    return source


def _post(client, path, payload):
    response = client.post(path, json=payload)
    response.raise_for_status()
    body = response.json()
    if not body.get("success"):
        raise RuntimeError(f"{path} 失败：{body.get('message')}")
    return body.get("data")


def _stage(client, info):
    source = _checked_source(info)
    with db_session() as db:
        existing = db.scalars(select(MarkdownFile).where(MarkdownFile.original_file_name == source.name)).all()
        if len(existing) > 1:
            raise RuntimeError(f"同名资料有多条，拒绝猜测：{source.name}")
        row = existing[0] if existing else None
        if row and (row.content_sha256 != info["sha256"]
                    or not Path(row.file_path).is_file()
                    or hashlib.sha256(Path(row.file_path).read_bytes()).hexdigest() != info["sha256"]):
            raise RuntimeError(f"已有同名资料内容不同，拒绝覆盖：{source.name}")
        file_id = row.id if row else None
        registered = bool(row and row.document_key)

    if file_id is None:
        with source.open("rb") as stream:
            response = client.post("/customer-service/md/upload", files={
                "file": (source.name, stream, "application/pdf")})
        response.raise_for_status()
        body = response.json()
        if not body.get("success"):
            raise RuntimeError(f"上传失败：{source.name}: {body.get('message')}")
        file_id = body["data"]["id"]
    if not registered:
        _post(client, "/customer-service/md/versions/register", {
            "id": file_id, "revisionLabel": info["revisionLabel"], "sourceUrl": info["sourceUrl"]})
    return file_id


def _verify_index(file_id, info):
    source = _checked_source(info)
    expected = read_documents(source, source.name)
    expected_pages = {part.metadata["pdfPage"] for part in expected}
    with db_session() as db:
        row = db.get(MarkdownFile, file_id)
        if row.status != 2 or row.source_url != info["sourceUrl"]:
            raise RuntimeError(f"文件状态或登记来源不匹配：{info['file']}")
        # A fast Worker may finish before /versions/register commits. Scope the
        # repair to this checked file and copy its already-registered source.
        db.execute(text("""UPDATE t_vector_store
                           SET metadata=(metadata::jsonb || jsonb_build_object('sourceUrl', CAST(:url AS text)))::json
                           WHERE metadata->>'mdStorageId'=:id
                             AND metadata->>'sourceUrl' IS DISTINCT FROM :url"""),
                   {"id": str(file_id), "url": info["sourceUrl"]})
        rows = db.execute(text("""SELECT (metadata->>'pdfPage')::int AS page,
                                           metadata->>'sourceUrl' AS source_url
                                    FROM t_vector_store WHERE metadata->>'mdStorageId'=:id"""),
                          {"id": str(file_id)}).all()
        if len(rows) != len(expected) or {part.page for part in rows} != expected_pages:
            raise RuntimeError(f"向量数或 PDF 页码覆盖不完整：{info['file']}")
        if any(part.source_url != info["sourceUrl"] for part in rows):
            raise RuntimeError(f"向量缺少原始资料链接：{info['file']}")
        return {"id": file_id, "name": info["file"], "pdfPages": info["pdfPages"],
                "indexedPages": len(expected_pages), "chunks": len(rows),
                "status": row.status, "reviewStatus": row.review_status,
                "processingWarnings": row.processing_warnings}


def main():
    entries = json.loads((MANUALS_DIR / "device_manuals.json").read_text(encoding="utf-8"))
    client = TestClient(app)
    login_client(client)
    try:
        staged = [(info, _stage(client, info)) for info in entries]
        deadline = time.monotonic() + settings.task_timeout_seconds * len(staged) + 120
        pending = dict((file_id, info) for info, file_id in staged)
        results = []
        while pending:
            for file_id, info in list(pending.items()):
                with db_session() as db:
                    row = db.get(MarkdownFile, file_id)
                    status, error, review = row.status, row.processing_error, row.review_status
                if status == 3:
                    raise RuntimeError(f"索引失败：{info['file']}: {error}")
                if status != 2:
                    continue
                result = _verify_index(file_id, info)
                if review in ("draft", "rejected"):
                    _post(client, "/customer-service/md/versions/submit", {"id": file_id})
                    result["reviewStatus"] = "pending_review"
                results.append(result)
                del pending[file_id]
                print(json.dumps(result, ensure_ascii=False), flush=True)
            if pending:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"等待 Worker 处理超时：{list(pending)}")
                time.sleep(10)
        print(json.dumps({"staged": len(results), "published": 0,
                          "nextStep": "由独立审核者完成质量评测和批准后才进入公开问答"},
                         ensure_ascii=False), flush=True)
    finally:
        client.post("/auth/logout")
        client.close()


if __name__ == "__main__":
    main()
