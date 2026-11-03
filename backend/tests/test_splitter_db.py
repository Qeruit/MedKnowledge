"""Opt-in test of reindexing and chunk provenance in the dedicated PostgreSQL database."""
from datetime import datetime
import os

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from app import knowledge, main
from app.config import settings
from app.db import MarkdownFile, db_session


@pytest.mark.skipif(os.getenv("RUN_UPLOAD_DB_TEST") != "1", reason="需要独立 PostgreSQL 测试库")
def test_reindex_persists_each_child_and_source(monkeypatch, tmp_path, admin_client):
    client, headers, user = admin_client
    def embeddings(values):
        assert all("安装流程" in value and "测试器械" in value for value in values)
        return [[1.0] + [0.0] * (settings.embedding_dimensions - 1) for _ in values]

    monkeypatch.setattr(knowledge, "embed_texts_sync", embeddings)
    source = "---\nversion: 1.2\nsource_url: https://example.com/manual.pdf\n---\n# 测试器械\n## 安装流程\n"
    source += "\n\n".join(f"测试步骤{i}：" + "保持段落信息完整，这些文字不是器械操作说明。" * 30 for i in range(15))
    source += "\n\n来源：原说明书 PDF 第 12 页。"
    path = tmp_path / "splitter-integration.md"
    path.write_text(source, encoding="utf-8")
    with db_session() as db:
        file = MarkdownFile(original_file_name=path.name, new_file_name=path.name,
                            file_path=str(path), file_size=path.stat().st_size, status=2,
                            create_time=datetime.now(), update_time=datetime.now(), owner_id=user.id)
        db.add(file)
        db.flush()
        file_id = file.id
    try:
        response = client.post("/customer-service/md/reindex", json={"id": file_id}, headers=headers)
        assert response.status_code == 200 and response.json()["success"]
        from app.worker import run_once
        assert run_once(file_id=file_id)
        with db_session() as db:
            assert db.get(MarkdownFile, file_id).status == 2
            rows = db.execute(text("SELECT content, metadata FROM t_vector_store WHERE metadata->>'mdStorageId'=:id"),
                              {"id": str(file_id)}).all()
            assert len(rows) > 1
            assert sorted(row.metadata["chunkIndex"] for row in rows) == list(range(len(rows)))
            assert all(row.metadata["documentVersion"] == "1.2" for row in rows)
            assert all(row.metadata["sourceUrl"] == "https://example.com/manual.pdf" for row in rows)
            assert all(row.metadata["pageReferences"] == ["PDF 第 12 页"] for row in rows)
    finally:
        with db_session() as db:
            db.execute(text("DELETE FROM t_vector_store WHERE metadata->>'mdStorageId'=:id"), {"id": str(file_id)})
            db.execute(text("DELETE FROM t_admin_audit WHERE file_id=:id"), {"id": file_id})
            db.execute(text("DELETE FROM t_ai_customer_service_md_storage WHERE id=:id"), {"id": file_id})
