"""Real Embedding + SSE smoke test on a temporary PDF; existing knowledge is preserved."""
import hashlib
import json
import subprocess
import sys
import secrets
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from app.config import PROJECT_ROOT, settings
from app.db import MarkdownFile, db_session
from scripts.generate_format_samples import generate
from scripts.auth_helpers import login_client


def main():
    paths = generate(PROJECT_ROOT / "data" / "format_samples")
    source = next(path for path in paths if path.suffix == ".pdf")
    body = source.read_bytes()
    sha = hashlib.sha256(body).hexdigest()
    reviewer = TestClient(app)
    reviewer_headers = login_client(reviewer)
    editor_name = "format_smoke_" + secrets.token_hex(6)
    editor_password = "Test-format-smoke-password-2026"
    created = reviewer.post("/admin/users", json={"username": editor_name,
        "password": editor_password, "role": "editor"}, headers=reviewer_headers)
    created.raise_for_status()
    editor_id = created.json()["data"]["id"]
    client = TestClient(app)
    login = client.post("/auth/login", json={"username": editor_name, "password": editor_password})
    login.raise_for_status()
    headers = {"X-CSRF-Token": login.json()["data"]["csrfToken"]}
    client.headers.update(headers)
    upload_id = file_id = stored_path = None
    try:
        response = client.post("/customer-service/md/uploads", json={
            "fileName": source.name, "fileSize": len(body), "sha256": sha}, headers=headers)
        response.raise_for_status()
        init = response.json()["data"]
        if init["instant"]:
            raise RuntimeError("同内容文件已经在库中，请使用新的测试文件；不会清理已有记录")
        upload_id = init["uploadId"]
        base = f"/customer-service/md/uploads/{upload_id}"
        client.put(base + "/chunks/0", content=body,
                   headers={**headers, "X-Chunk-Sha256": sha}).raise_for_status()
        result = client.post(base + "/complete", headers=headers)
        result.raise_for_status()
        if result.json()["data"]["instant"]:
            raise RuntimeError("完成时复用了已有记录，停止测试且不删除已有资料")
        file_id = result.json()["data"]["fileId"]
        # A separate Python process, not the API or TestClient background thread.
        subprocess.run([sys.executable, "-c",
            "from app.worker import run_once; assert run_once(worker_id='real-model-smoke', file_id=" + str(int(file_id)) + ")"],
            cwd=PROJECT_ROOT, check=True, timeout=settings.task_timeout_seconds + 60)
        with db_session() as db:
            row = db.get(MarkdownFile, file_id)
            stored_path = Path(row.file_path)
            assert row.status == 2, row.processing_error
            vectors = db.execute(text("SELECT metadata FROM t_vector_store WHERE metadata->>'mdStorageId'=:id"),
                                 {"id": str(file_id)}).scalars().all()
            assert {metadata["pdfPage"] for metadata in vectors} == {1, 2}
        client.post("/customer-service/md/versions/register", json={"id": file_id,
            "revisionLabel": "smoke v1"}, headers=headers).raise_for_status()
        client.post("/customer-service/md/versions/submit", json={"id": file_id}, headers=headers).raise_for_status()
        reviewer.post("/customer-service/md/versions/review", json={"id": file_id,
            "approve": True, "note": "temporary smoke test"}, headers=reviewer_headers).raise_for_status()
        answer = client.post("/customer-service/completion", json={
            "message": "What is the sample identifier in the file reader test?", "fileId": file_id})
        answer.raise_for_status()
        answer_text = "".join(json.loads(line[6:]).get("v", "")
                              for line in answer.text.splitlines() if line.startswith("data: "))
        assert "event: sources" in answer.text and "TEST-FORMAT-2026" in answer_text, answer_text[:1000]
        print("PASS: PDF upload, real Embedding, page metadata, retrieval and SSE answer")
    finally:
        client.post("/auth/logout")
        client.close()
        reviewer.post("/auth/logout")
        reviewer.close()
        with db_session() as db:
            db.execute(text("DELETE FROM t_worker_heartbeat WHERE worker_id='real-model-smoke'"))
            if upload_id:
                db.execute(text("DELETE FROM t_upload_chunk WHERE upload_id=:id"), {"id": UUID(upload_id)})
                db.execute(text("DELETE FROM t_upload_session WHERE id=:id"), {"id": UUID(upload_id)})
            if file_id:
                db.execute(text("DELETE FROM t_vector_store WHERE metadata->>'mdStorageId'=:id"), {"id": str(file_id)})
                db.execute(text("DELETE FROM t_admin_audit WHERE file_id=:id"), {"id": file_id})
                db.execute(text("DELETE FROM t_ai_customer_service_md_storage WHERE id=:id"), {"id": file_id})
            db.execute(text("DELETE FROM t_admin_audit WHERE actor_id=:id"), {"id": editor_id})
            db.execute(text("DELETE FROM t_auth_session WHERE user_id=:id"), {"id": editor_id})
            db.execute(text("DELETE FROM t_user WHERE id=:id"), {"id": editor_id})
        if stored_path and stored_path.resolve().is_relative_to(settings.md_storage_path):
            stored_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
