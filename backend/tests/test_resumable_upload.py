"""Opt-in PostgreSQL integration test: RUN_UPLOAD_DB_TEST=1 python -m pytest tests/test_resumable_upload.py."""

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import main, resumable_upload
from app.config import settings
from app.db import db_session


pytestmark = pytest.mark.skipif(os.getenv("RUN_UPLOAD_DB_TEST") != "1", reason="需要独立 PostgreSQL 测试库")


def test_resumable_and_instant_upload(monkeypatch, tmp_path, admin_client):
    monkeypatch.setattr(resumable_upload, "settings", SimpleNamespace(md_storage_path=tmp_path))
    client, headers, user = admin_client
    content = b"# test\n" + b"a" * resumable_upload.CHUNK_SIZE + b"\nend\n"
    digest = hashlib.sha256(content).hexdigest()
    upload_id = None
    file_id = None
    try:
        payload = {"fileName": "upload-integration-test.md", "fileSize": len(content), "sha256": digest}
        init = client.post("/customer-service/md/uploads", json=payload, headers=headers)
        assert init.status_code == 200
        data = init.json()["data"]
        assert data["instant"] is False and data["totalChunks"] == 2
        upload_id = data["uploadId"]
        base = f"/customer-service/md/uploads/{upload_id}"
        assert client.post(base + "/complete", headers=headers).status_code == 409

        first = content[:data["chunkSize"]]
        second = content[data["chunkSize"]:]
        bad = client.put(base + "/chunks/0", content=first,
                         headers={**headers, "X-Chunk-Sha256": "0" * 64})
        assert bad.status_code == 422
        good = client.put(base + "/chunks/0", content=first,
                          headers={**headers, "X-Chunk-Sha256": hashlib.sha256(first).hexdigest()})
        assert good.status_code == 200
        assert client.get(base, headers=headers).json()["data"]["uploadedChunks"] == [0]
        resumed = client.post("/customer-service/md/uploads", json=payload, headers=headers).json()["data"]
        assert resumed["uploadId"] == upload_id and resumed["uploadedChunks"] == [0]
        assert client.put(base + "/chunks/1", content=second,
                          headers={**headers, "X-Chunk-Sha256": hashlib.sha256(second).hexdigest()}).status_code == 200
        completed = client.post(base + "/complete", headers=headers).json()["data"]
        assert completed["status"] == "completed" and not completed["instant"]
        file_id = completed["fileId"]
        assert client.post(base + "/complete", headers=headers).json()["data"]["alreadyCompleted"]
        with db_session() as db:
            path = Path(db.execute(text("SELECT file_path FROM t_ai_customer_service_md_storage WHERE id=:id"),
                                   {"id": file_id}).scalar_one())
            assert path.read_bytes() == content
            db.execute(text("DELETE FROM t_index_job WHERE file_id=:id"), {"id": file_id})
            db.execute(text("UPDATE t_ai_customer_service_md_storage SET status=2 WHERE id=:id"), {"id": file_id})
        instant = client.post("/customer-service/md/uploads", json=payload, headers=headers).json()["data"]
        assert instant["instant"] and instant["fileId"] == file_id
    finally:
        with db_session() as db:
            if upload_id:
                db.execute(text("DELETE FROM t_upload_chunk WHERE upload_id=:id"), {"id": UUID(upload_id)})
                db.execute(text("DELETE FROM t_upload_session WHERE id=:id"), {"id": UUID(upload_id)})
            if file_id:
                db.execute(text("DELETE FROM t_admin_audit WHERE file_id=:id"), {"id": file_id})
                db.execute(text("DELETE FROM t_ai_customer_service_md_storage WHERE id=:id"), {"id": file_id})
