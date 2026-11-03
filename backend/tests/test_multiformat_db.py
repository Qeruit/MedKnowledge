"""Full upload/parse/index/delete lifecycle, using real parsers and test embeddings."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from app import knowledge, main, resumable_upload
from app.config import PROJECT_ROOT, settings
from app.db import MarkdownFile, db_session
from scripts.generate_format_samples import generate
from app.worker import run_once


@pytest.mark.skipif(os.getenv("RUN_UPLOAD_DB_TEST") != "1", reason="需要独立 PostgreSQL 测试库")
@pytest.mark.parametrize("extension", ["md", "pdf", "docx", "txt", "doc", "broken-pdf"])
def test_upload_parse_vectors_delete(monkeypatch, tmp_path, extension, admin_client):
    if extension == "doc" and not settings.tika_server_url:
        pytest.skip("旧版 DOC 的集成测试需要 Tika")
    generate(tmp_path / "input")
    path = (PROJECT_ROOT / "data" / "apache_tika_testWORD.doc" if extension == "doc" else
            tmp_path / "input" / ("broken.pdf" if extension == "broken-pdf" else f"sample.{extension}"))
    if extension == "broken-pdf":
        path.write_bytes(b"%PDF-1.7\nthis is not a valid PDF body")
    selected_settings = replace(settings, md_storage_path=tmp_path / "storage")
    monkeypatch.setattr(main, "settings", selected_settings)
    monkeypatch.setattr(resumable_upload, "settings", selected_settings)
    monkeypatch.setattr(knowledge, "embed_texts_sync", lambda values: [
        [1.0] + [0.0] * (settings.embedding_dimensions - 1) for _ in values])
    content = path.read_bytes()
    payload = {"fileName": path.name, "fileSize": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    client, headers, user = admin_client
    upload_id, file_id = None, None
    try:
        response = client.post("/customer-service/md/uploads", json=payload, headers=headers)
        assert response.status_code == 200
        init = response.json()["data"]
        assert not init["instant"]
        upload_id = init["uploadId"]
        base = f"/customer-service/md/uploads/{upload_id}"
        assert client.put(base + "/chunks/0", content=content,
                          headers={**headers, "X-Chunk-Sha256": payload["sha256"]}).status_code == 200
        completed = client.post(base + "/complete", headers=headers)
        assert completed.status_code == 200
        file_id = completed.json()["data"]["fileId"]
        assert run_once(file_id=file_id)
        with db_session() as db:
            row = db.get(MarkdownFile, file_id)
            stored_path = Path(row.file_path)
            assert stored_path.suffix == path.suffix and stored_path.read_bytes() == content
            vectors = db.execute(text("SELECT content,metadata FROM t_vector_store WHERE metadata->>'mdStorageId'=:id"),
                                 {"id": str(file_id)}).all()
            if extension == "broken-pdf":
                assert row.status == 3 and "PDF" in row.processing_error and not vectors
            else:
                assert row.status == 2 and not row.processing_error and vectors
                assert all(v.metadata["fileFormat"] == ("markdown" if extension == "md" else "text" if extension == "txt" else extension)
                           for v in vectors)
                expected = "Sample Word" if extension == "doc" else "TEST-FORMAT-2026"
                assert expected in "\n".join(v.content for v in vectors)
                if extension == "pdf":
                    assert {v.metadata["pdfPage"] for v in vectors} == {1, 2}
        if extension != "broken-pdf":
            instant = client.post("/customer-service/md/uploads", json=payload, headers=headers).json()["data"]
            assert instant["instant"] and instant["fileId"] == file_id
        deleted = client.post("/customer-service/md/delete", json={"id": file_id}, headers=headers)
        assert deleted.status_code == 200 and deleted.json()["success"] and not stored_path.exists()
        with db_session() as db:
            assert db.get(MarkdownFile, file_id) is None
            assert db.scalar(text("SELECT count(*) FROM t_vector_store WHERE metadata->>'mdStorageId'=:id"),
                             {"id": str(file_id)}) == 0
    finally:
        with db_session() as db:
            if upload_id:
                db.execute(text("DELETE FROM t_upload_chunk WHERE upload_id=:id"), {"id": UUID(upload_id)})
                db.execute(text("DELETE FROM t_upload_session WHERE id=:id"), {"id": UUID(upload_id)})
            if file_id:
                db.execute(text("DELETE FROM t_vector_store WHERE metadata->>'mdStorageId'=:id"), {"id": str(file_id)})
                db.execute(text("DELETE FROM t_admin_audit WHERE file_id=:id"), {"id": file_id})
                db.execute(text("DELETE FROM t_ai_customer_service_md_storage WHERE id=:id"), {"id": file_id})
