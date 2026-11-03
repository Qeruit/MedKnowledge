"""Role/access integration tests against the dedicated database; no real model calls."""
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import secrets
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, select, text

from app import knowledge, main, quality_queue, release_quality, resumable_upload
from app.auth import COOKIE, token_hash
from app.config import settings
from app.db import AuthSession, MarkdownFile, UploadSession, User, db_session
from app.llm import vector_literal

pytestmark = pytest.mark.skipif(os.getenv("RUN_UPLOAD_DB_TEST") != "1", reason="需要独立 PostgreSQL 权限测试")
PASSWORD = "Test-permissions-password-2026"


def complete_quality_job(client, headers, file_id):
    enqueued = client.post("/customer-service/md/quality/run", json={"id": file_id}, headers=headers)
    assert enqueued.status_code == 200 and enqueued.json()["data"]["status"] == "queued", enqueued.text
    job = quality_queue.claim_quality("test-quality-worker", candidate_id=file_id)
    assert job and str(job["id"]) == enqueued.json()["data"]["jobId"]
    quality_queue.process_quality(job)
    status = client.post("/customer-service/md/quality/status", json={"id": file_id}, headers=headers).json()["data"]
    assert status["status"] == "succeeded" and status["cursor"] == status["total"]
    return client.post("/customer-service/md/quality/latest", json={"id": file_id}, headers=headers)


@pytest.fixture
def roles(admin_client, monkeypatch, tmp_path):
    admin, admin_headers, admin_user = admin_client
    chosen = replace(settings, md_storage_path=tmp_path / "storage")
    monkeypatch.setattr(main, "settings", chosen)
    monkeypatch.setattr(resumable_upload, "settings", chosen)
    ids = [admin_user.id]
    values = {"admin": (admin, admin_headers, {"id": admin_user.id, "role": "admin", "username": admin_user.username})}
    for name, role in (("viewer", "viewer"), ("editor", "editor"), ("other", "editor")):
        username = name + "_" + secrets.token_hex(6)
        response = admin.post("/admin/users", json={"username": username, "password": PASSWORD, "role": role}, headers=admin_headers)
        assert response.status_code == 200
        info = response.json()["data"]
        ids.append(info["id"])
        client = TestClient(main.app)
        login = client.post("/auth/login", json={"username": username, "password": PASSWORD})
        assert login.status_code == 200 and "httponly" in login.headers["set-cookie"].lower()
        assert "samesite=strict" in login.headers["set-cookie"].lower()
        values[name] = (client, {"X-CSRF-Token": login.json()["data"]["csrfToken"]}, info)
    monkeypatch.setattr(knowledge, "embed_texts_sync", lambda texts: [
        [1.] + [0.] * (settings.embedding_dimensions - 1) for _ in texts])
    try:
        yield values
    finally:
        with db_session() as db:
            files = db.scalars(select(MarkdownFile).where(MarkdownFile.owner_id.in_(ids))).all()
            for file in files:
                db.execute(text("DELETE FROM t_vector_store WHERE metadata->>'mdStorageId'=:id"), {"id": str(file.id)})
                db.delete(file)
            db.execute(text("DELETE FROM t_release_eval_case WHERE document_key IN "
                            "(SELECT document_key FROM t_ai_customer_service_md_storage WHERE owner_id=ANY(:ids))"),
                       {"ids": ids})
            db.execute(delete(UploadSession).where(UploadSession.owner_id.in_(ids)))
            db.execute(delete(AuthSession).where(AuthSession.user_id.in_(ids)))
            db.execute(text("DELETE FROM t_admin_audit WHERE actor_id=ANY(:ids)"), {"ids": ids})
            for name in ("viewer", "editor", "other"):
                info = values[name][2]
                db.execute(text("DELETE FROM t_login_limit WHERE key=:key"), {"key": token_hash(info["username"] + ":testclient")})
            db.execute(delete(User).where(User.id.in_(ids[1:])))


def test_unauthenticated_and_viewer_cannot_manage(roles):
    anonymous = TestClient(main.app)
    assert anonymous.post("/customer-service/completion", json={"message": "test"}).status_code == 401
    assert anonymous.post("/customer-service/md/list", json={}, headers={"X-Admin-Token": settings.admin_token}).status_code == 401
    viewer, headers, _ = roles["viewer"]
    for path, payload in (("/customer-service/md/list", {}), ("/customer-service/md/update", {"id": 1}),
                          ("/customer-service/md/delete", {"id": 1}), ("/admin/users", {"username": "nope", "password": PASSWORD})):
        assert viewer.post(path, json=payload, headers=headers).status_code == 403
    assert viewer.get("/admin/users").status_code == 403


def test_csrf_origin_sessions_and_account_revocation(roles):
    editor, headers, info = roles["editor"]
    assert editor.post("/customer-service/md/list", json={}).status_code == 403
    assert editor.post("/customer-service/md/list", json={}, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    assert editor.post("/auth/login", json={"username": info["username"], "password": PASSWORD},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    with db_session() as db:
        session = db.get(AuthSession, token_hash(editor.cookies.get(COOKIE)))
        assert session and session.token_hash != editor.cookies.get(COOKIE)
    admin, ah, _ = roles["admin"]
    response = admin.post("/admin/users/update", json={"id": info["id"], "role": "viewer", "isActive": True}, headers=ah)
    assert response.status_code == 200
    assert editor.get("/auth/me").status_code == 401
    login = editor.post("/auth/login", json={"username": info["username"], "password": PASSWORD})
    assert login.status_code == 200
    assert editor.post("/customer-service/md/list", json={},
                       headers={"X-CSRF-Token": login.json()["data"]["csrfToken"]}).status_code == 403


def test_editor_ownership_upload_isolation_and_actor_audit(roles):
    editor, headers, info = roles["editor"]
    other, oh, other_info = roles["other"]
    content = b"## permission test\nOWN-FILE-ONLY"
    response = editor.post("/customer-service/md/upload", files={"file": ("owned.md", content)}, headers=headers)
    assert response.status_code == 200 and response.json()["success"]
    file_id = response.json()["data"]["id"]
    own = editor.post("/customer-service/md/list", json={}, headers=headers).json()
    assert own["total"] == 1 and own["data"][0]["ownerId"] == info["id"]
    assert not own["data"][0]["isPublished"]
    assert other.post("/customer-service/md/list", json={}, headers=oh).json()["total"] == 0
    assert other.post("/customer-service/md/update", json={"id": file_id}, headers=oh).status_code == 404
    assert other.post("/customer-service/md/delete", json={"id": file_id}, headers=oh).status_code == 404
    payload = {"fileName": "owned.md", "fileSize": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    init = other.post("/customer-service/md/uploads", json=payload, headers=oh).json()["data"]
    assert not init["instant"]
    url = "/customer-service/md/uploads/" + init["uploadId"]
    assert editor.get(url).status_code == 404
    assert editor.put(url + "/chunks/0", content=content,
                      headers={**headers, "X-Chunk-Sha256": payload["sha256"]}).status_code == 404
    assert editor.post(url + "/complete", json={}, headers=headers).status_code == 404
    events = editor.get("/customer-service/md/audit").json()["data"]
    assert any(e["action"] == "upload" and e["actorId"] == info["id"] and e["actorName"] == info["username"] for e in events)
    assert not any(e["actorId"] == info["id"] for e in other.get("/customer-service/md/audit").json()["data"])


def test_retrieval_uses_only_granted_published_sources_and_revoke_is_immediate(roles, monkeypatch):
    admin, ah, admin_info = roles["admin"]
    viewer, vh, viewer_info = roles["viewer"]
    file_ids = []
    with db_session() as db:
        for name, published in (("ALLOWED", True), ("DENIED", True), ("DRAFT", False)):
            row = MarkdownFile(original_file_name=name + ".md", new_file_name=name + ".md", file_path="test-only",
                               file_size=10, status=2, owner_id=admin_info["id"], is_published=published,
                               create_time=datetime.now(), update_time=datetime.now())
            db.add(row); db.flush(); file_ids.append(row.id)
            db.execute(text("INSERT INTO t_vector_store(id,content,metadata,embedding) VALUES(:id,:content,CAST(:metadata AS json),CAST(:vector AS vector))"), {
                "id": uuid4(), "content": "## Test fact\n" + name + "-SECRET-MARKER",
                "metadata": json.dumps({"mdStorageId": row.id, "originalFileName": row.original_file_name}),
                "vector": vector_literal([1.] + [0.] * (settings.embedding_dimensions - 1))})
    for file_id in (file_ids[0], file_ids[2]):
        assert admin.post("/customer-service/md/grants", json={"id": file_id, "userIds": [viewer_info["id"]]}, headers=ah).status_code == 200
    calls = []
    async def embed(values):
        calls.append(values)
        return [[1.] + [0.] * (settings.embedding_dimensions - 1) for _ in values]
    async def answer(messages, model, temperature):
        assert "ALLOWED-SECRET-MARKER" in messages[0]["content"]
        assert "DENIED-SECRET-MARKER" not in messages[0]["content"] and "DRAFT-SECRET-MARKER" not in messages[0]["content"]
        yield "ALLOWED-SECRET-MARKER"
    monkeypatch.setattr(knowledge, "embed_texts", embed)
    monkeypatch.setattr(main, "chat_chunks", answer)
    result = viewer.post("/customer-service/completion", json={"message": "What is the test fact?", "fileId": file_ids[0]}, headers=vh)
    assert result.status_code == 200 and "ALLOWED-SECRET-MARKER" in result.text
    assert "DENIED.md" not in result.text and "DRAFT.md" not in result.text
    assert [r["id"] for r in viewer.get("/customer-service/md/available").json()["data"]] == [file_ids[0]]
    assert admin.post("/customer-service/md/grants", json={"id": file_ids[0], "userIds": []}, headers=ah).status_code == 200
    before = len(calls)
    result = viewer.post("/customer-service/completion", json={"message": "What is the test fact?", "fileId": file_ids[0]}, headers=vh)
    assert result.status_code == 404 and len(calls) == before
    for forbidden_id in file_ids[1:]:
        result = viewer.post("/customer-service/completion", json={"message": "What is the test fact?", "fileId": forbidden_id}, headers=vh)
        assert result.status_code == 404 and len(calls) == before


def test_lexical_recall_recovers_evidence_outside_vector_top_16_without_leaking(roles, monkeypatch):
    import asyncio
    admin, ah, admin_info = roles["admin"]
    _, _, viewer_info = roles["viewer"]
    with db_session() as db:
        allowed = MarkdownFile(original_file_name="hybrid-allowed.md", new_file_name="hybrid-allowed.md",
                               file_path="test-only", file_size=10, status=2, owner_id=admin_info["id"],
                               is_published=True, create_time=datetime.now(), update_time=datetime.now())
        denied = MarkdownFile(original_file_name="hybrid-denied.md", new_file_name="hybrid-denied.md",
                              file_path="test-only", file_size=10, status=2, owner_id=admin_info["id"],
                              is_published=True, create_time=datetime.now(), update_time=datetime.now())
        db.add_all([allowed, denied]); db.flush()
        for index in range(20):
            db.execute(text("""INSERT INTO t_vector_store(id,content,metadata,embedding)
                VALUES(:id,:content,CAST(:metadata AS json),CAST(:vector AS vector))"""), {
                "id": uuid4(), "content": f"## Unrelated page {index}\nGeneral device notes",
                "metadata": json.dumps({"mdStorageId": allowed.id, "originalFileName": allowed.original_file_name}),
                "vector": vector_literal([1.] + [0.] * (settings.embedding_dimensions - 1))})
        for row, marker in ((allowed, "AUTHORIZED"), (denied, "PRIVATE")):
            db.execute(text("""INSERT INTO t_vector_store(id,content,metadata,embedding)
                VALUES(:id,:content,CAST(:metadata AS json),CAST(:vector AS vector))"""), {
                "id": uuid4(), "content": f"## Storage temperature\n{marker} storage temperature is 20 C",
                "metadata": json.dumps({"mdStorageId": row.id, "originalFileName": row.original_file_name}),
                "vector": vector_literal([.8, .6] + [0.] * (settings.embedding_dimensions - 2))})
        allowed_id, denied_id = allowed.id, denied.id
    assert admin.post("/customer-service/md/grants", json={"id": allowed_id, "userIds": [viewer_info["id"]]},
                      headers=ah).status_code == 200
    async def embed(values):
        return [[1.] + [0.] * (settings.embedding_dimensions - 1) for _ in values]
    monkeypatch.setattr(knowledge, "embed_texts", embed)
    trace = {}
    results = asyncio.run(knowledge.find_context("storage temperature", user_id=viewer_info["id"],
                                                 role="viewer", trace=trace))
    assert any("AUTHORIZED" in result["content"] for result in results)
    assert not any("PRIVATE" in result["content"] for result in results)
    assert any("lexical" in row["channels"] and "AUTHORIZED" in row["content"]
               for row in trace["candidates"][0]["rows"])
    assert not any("PRIVATE" in row["content"] for row in trace["candidates"][0]["rows"])


def test_selected_document_scopes_vector_lexical_sources_and_answer(roles, monkeypatch):
    admin, ah, admin_info = roles["admin"]
    viewer, vh, viewer_info = roles["viewer"]
    file_ids = []
    with db_session() as db:
        for name, temperature in (("DEVICE_A", "18 C"), ("DEVICE_B", "42 C")):
            row = MarkdownFile(original_file_name=name + ".md", new_file_name=name + ".md",
                               file_path="test-only", file_size=10, status=2, owner_id=admin_info["id"],
                               is_published=True, create_time=datetime.now(), update_time=datetime.now())
            db.add(row); db.flush(); file_ids.append(row.id)
            db.execute(text("""INSERT INTO t_vector_store(id,content,metadata,embedding)
                VALUES(:id,:content,CAST(:metadata AS json),CAST(:vector AS vector))"""), {
                "id": uuid4(), "content": f"## Storage temperature\n{name} storage temperature is {temperature}",
                "metadata": json.dumps({"mdStorageId": row.id, "originalFileName": row.original_file_name}),
                "vector": vector_literal([1.] + [0.] * (settings.embedding_dimensions - 1))})
    for file_id in file_ids:
        assert admin.post("/customer-service/md/grants", json={"id": file_id, "userIds": [viewer_info["id"]]},
                          headers=ah).status_code == 200
    async def embed(values):
        return [[1.] + [0.] * (settings.embedding_dimensions - 1) for _ in values]
    async def answer(messages, model, temperature):
        evidence = messages[0]["content"]
        assert ("DEVICE_A" in evidence) != ("DEVICE_B" in evidence)
        yield "18 C" if "DEVICE_A" in evidence else "42 C"
    monkeypatch.setattr(knowledge, "embed_texts", embed)
    monkeypatch.setattr(main, "chat_chunks", answer)
    for client, headers in ((viewer, vh), (admin, ah)):
        for file_id, included, excluded, value in (
                (file_ids[0], "DEVICE_A", "DEVICE_B", "18 C"),
                (file_ids[1], "DEVICE_B", "DEVICE_A", "42 C")):
            response = client.post("/customer-service/completion", json={
                "message": "storage temperature", "fileId": file_id}, headers=headers)
            assert response.status_code == 200, response.text
            assert included in response.text and excluded not in response.text
            assert value in response.text
            trace = {}
            import asyncio
            evidence = asyncio.run(knowledge.find_context("storage temperature", user_id=viewer_info["id"],
                                                         role="viewer", file_id=file_id, trace=trace))
            assert evidence and all(included in row["content"] for row in evidence)
            assert all(included in row["content"] for row in trace["candidates"][0]["rows"])
    assert admin.post("/customer-service/md/grants", json={"id": file_ids[0], "userIds": []},
                      headers=ah).status_code == 200
    revoked = viewer.post("/customer-service/completion", json={
        "message": "storage temperature", "fileId": file_ids[0]}, headers=vh)
    assert revoked.status_code == 404


def test_durable_followup_is_file_scoped_and_never_uses_old_answer_as_evidence(roles, monkeypatch):
    from app.chat_history import finish_conversation, open_conversation, prune_expired_conversations

    admin, ah, admin_info = roles["admin"]
    viewer, vh, viewer_info = roles["viewer"]
    other, oh, other_info = roles["other"]
    file_ids = []
    with db_session() as db:
        for name, fact in (("ActaStim-S", "每日 24 小时"), ("OtherDevice-X", "每日 2 小时")):
            row = MarkdownFile(original_file_name=name + ".md", new_file_name=name + ".md",
                               file_path="test-only", file_size=10, status=2, owner_id=admin_info["id"],
                               is_published=True, create_time=datetime.now(), update_time=datetime.now())
            db.add(row); db.flush(); file_ids.append(row.id)
            db.execute(text("""INSERT INTO t_vector_store(id,content,metadata,embedding)
                VALUES(:id,:content,CAST(:metadata AS json),CAST(:vector AS vector))"""), {
                "id": uuid4(), "content": f"## {name} 每天使用多久\n{name} {fact}。存储温度 20 C。",
                "metadata": json.dumps({"mdStorageId": row.id, "originalFileName": row.original_file_name}),
                "vector": vector_literal([1.] + [0.] * (settings.embedding_dimensions - 1))})
    for file_id in file_ids:
        assert admin.post("/customer-service/md/grants", json={"id": file_id, "userIds": [viewer_info["id"]]},
                          headers=ah).status_code == 200
    queries = []
    async def embed(values):
        queries.extend(values)
        return [[1.] + [0.] * (settings.embedding_dimensions - 1) for _ in values]
    async def answer(messages, model, temperature):
        prompt = json.dumps(messages, ensure_ascii=False)
        assert "MODEL_ONLY_MARKER" not in prompt
        current_question = messages[-1]["content"].split("本轮问题：")[-1]
        yield "MODEL_ONLY_MARKER" if "存储温度" in current_question else "每日 24 小时"
    monkeypatch.setattr(knowledge, "embed_texts", embed)
    monkeypatch.setattr(main, "chat_chunks", answer)

    first = viewer.post("/customer-service/completion", json={
        "message": "ActaStim-S 的存储温度是多少？", "fileId": file_ids[0]}, headers=vh)
    assert first.status_code == 200 and "event: close" in first.text
    first_frames = [json.loads(line[6:]) for line in first.text.splitlines() if line.startswith("data: ")]
    conversation_id = next(frame["id"] for frame in first_frames if "id" in frame)
    assert any("MODEL_ONLY_MARKER" in frame.get("v", "") for frame in first_frames)
    second = viewer.post("/customer-service/completion", json={
        "message": "那它每天使用多久？", "fileId": file_ids[0], "conversationId": conversation_id}, headers=vh)
    assert second.status_code == 200 and "每日 24 小时" in second.text
    assert "OtherDevice-X" not in second.text
    assert any("ActaStim-S" in query and "那它每天使用多久" in query for query in queries)
    assert "MODEL_ONLY_MARKER" not in " ".join(queries)
    detail = viewer.get(f"/customer-service/conversations/{conversation_id}")
    assert detail.status_code == 200
    turns = detail.json()["data"]["turns"]
    assert len(turns) == 2 and turns[1]["question"] == "那它每天使用多久？"
    assert turns[0]["sources"] and all(card["fileName"] == "ActaStim-S.md" for turn in turns
                                        for card in turn["sources"])
    # Even another user who can read the *same file* cannot read this session.
    assert admin.post("/customer-service/md/grants", json={"id": file_ids[0],
                      "userIds": [viewer_info["id"], other_info["id"]]}, headers=ah).status_code == 200
    assert other.get(f"/customer-service/conversations/{conversation_id}").status_code == 404
    assert other.post("/customer-service/completion", json={
        "message": "那它每天使用多久？", "fileId": file_ids[0],
        "conversationId": conversation_id}, headers=oh).status_code == 404

    switched = viewer.post("/customer-service/completion", json={
        "message": "每天使用多久？", "fileId": file_ids[1], "conversationId": conversation_id}, headers=vh)
    assert switched.status_code == 409
    assert "OtherDevice-X" not in switched.text
    new_scope = viewer.post("/customer-service/completion", json={
        "message": "OtherDevice-X 每天使用多久？", "fileId": file_ids[1]}, headers=vh)
    assert new_scope.status_code == 200 and "OtherDevice-X.md" in new_scope.text
    assert conversation_id not in new_scope.text

    # A separate process may reclaim an expired turn; a stale writer cannot append it.
    first_claim = uuid4()
    direct_id, _ = open_conversation(admin_info["id"], "admin", file_ids[0], None, first_claim)
    with pytest.raises(Exception) as busy:
        open_conversation(admin_info["id"], "admin", file_ids[0], direct_id, uuid4())
    assert busy.value.status_code == 409
    with db_session() as db:
        db.execute(text("UPDATE t_chat_conversation SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"),
                   {"id": direct_id})
    second_claim = uuid4()
    assert open_conversation(admin_info["id"], "admin", file_ids[0], direct_id, second_claim)[0] == direct_id
    assert not finish_conversation(direct_id, first_claim, question="old", answer="stale")
    assert finish_conversation(direct_id, second_claim, question="new", answer="fresh")
    assert [turn["question"] for turn in admin.get(f"/customer-service/conversations/{direct_id}")
            .json()["data"]["turns"]] == ["new"]
    with db_session() as db:
        db.execute(text("UPDATE t_chat_conversation SET expires_at=clock_timestamp()-interval '1 second' WHERE id=:id"),
                   {"id": direct_id})
    assert admin.get(f"/customer-service/conversations/{direct_id}").status_code == 410
    with pytest.raises(Exception) as expired:
        open_conversation(admin_info["id"], "admin", file_ids[0], direct_id, uuid4())
    assert expired.value.status_code == 410
    assert prune_expired_conversations() >= 1
    assert admin.get(f"/customer-service/conversations/{direct_id}").status_code == 404

    assert admin.post("/customer-service/md/grants", json={"id": file_ids[0], "userIds": []},
                      headers=ah).status_code == 200
    before = len(queries)
    assert viewer.get(f"/customer-service/conversations/{conversation_id}").status_code == 404
    revoked = viewer.post("/customer-service/completion", json={
        "message": "那它每天使用多久？", "fileId": file_ids[0], "conversationId": conversation_id}, headers=vh)
    assert revoked.status_code == 404 and len(queries) == before


def test_no_evidence_turn_is_saved_but_failed_generation_is_not(roles, monkeypatch):
    viewer, vh, viewer_info = roles["viewer"]
    with db_session() as db:
        row = MarkdownFile(original_file_name="single.md", new_file_name="single.md",
                           file_path="test-only", file_size=10, status=2, owner_id=viewer_info["id"],
                           is_published=True, create_time=datetime.now(), update_time=datetime.now())
        db.add(row); db.flush(); file_id = row.id
    async def empty(*_, **__):
        return []
    monkeypatch.setattr(main, "find_context", empty)
    first = viewer.post("/customer-service/completion", json={"message": "资料里没有的问题", "fileId": file_id},
                        headers=vh)
    assert first.status_code == 200 and "不足以回答" in first.text
    conversation_id = next(json.loads(line[6:])["id"] for line in first.text.splitlines()
                           if line.startswith("data: ") and '"id"' in line)
    detail = viewer.get(f"/customer-service/conversations/{conversation_id}").json()["data"]
    assert len(detail["turns"]) == 1 and detail["turns"][0]["sources"] == []

    async def evidence(*_, **__):
        return [{"content": "## fact\nA checked statement", "metadata": {"originalFileName": "single.md"}}]
    async def failure(*_):
        raise RuntimeError("simulated model failure")
        yield
    monkeypatch.setattr(main, "find_context", evidence)
    monkeypatch.setattr(main, "chat_chunks", failure)
    failed = viewer.post("/customer-service/completion", json={
        "message": "继续问", "fileId": file_id, "conversationId": conversation_id}, headers=vh)
    assert failed.status_code == 200 and "event: error" in failed.text
    assert len(viewer.get(f"/customer-service/conversations/{conversation_id}").json()["data"]["turns"]) == 1


def test_reviewed_versions_switch_retrieval_and_rollback_without_mixing(roles, monkeypatch):
    admin, ah, _ = roles["admin"]
    editor, eh, editor_info = roles["editor"]
    viewer, vh, viewer_info = roles["viewer"]
    monkeypatch.setattr(release_quality, "require_llm", lambda embedding=False: None)
    async def eval_answer(messages, model, temperature):
        yield "VERSION_1_ONLY" if "VERSION_1_ONLY" in messages[0]["content"] else "VERSION_2_ONLY"
    monkeypatch.setattr(release_quality, "chat_chunks", eval_answer)
    async def eval_embed(values):
        return [[1.] + [0.] * (settings.embedding_dimensions - 1) for _ in values]
    monkeypatch.setattr(knowledge, "embed_texts", eval_embed)
    monkeypatch.setattr(knowledge, "select_relevant_context", lambda question, rows: [row.content for row in rows])
    file_ids = []
    with db_session() as db:
        for number in (1, 2):
            row = MarkdownFile(original_file_name=f"version-{number}.md", new_file_name=f"version-{number}.md",
                               file_path="test-only", file_size=10, status=2, owner_id=editor_info["id"],
                               content_sha256=str(number) * 64, is_published=False,
                               create_time=datetime.now(), update_time=datetime.now())
            db.add(row)
            db.flush()
            file_ids.append(row.id)
            db.execute(text("""INSERT INTO t_vector_store(id,content,metadata,embedding)
                VALUES(:id,:content,CAST(:metadata AS json),CAST(:vector AS vector))"""), {
                "id": uuid4(), "content": f"## Test therapy fact\nVERSION_{number}_ONLY",
                "metadata": json.dumps({"mdStorageId": row.id, "originalFileName": row.original_file_name}),
                "vector": vector_literal([1.] + [0.] * (settings.embedding_dimensions - 1))})
    base = "/customer-service/md/versions"
    assert editor.post("/customer-service/md/publish", json={"id": file_ids[0], "isPublished": True}, headers=eh).status_code == 409
    registered = editor.post(base + "/register", json={"id": file_ids[0], "revisionLabel": "initial"}, headers=eh)
    assert registered.status_code == 200, registered.text
    document_key = registered.json()["data"]["documentKey"]
    assert registered.json()["data"]["version"] == 1
    assert editor.post(base + "/submit", json={"id": file_ids[0]}, headers=eh).status_code == 200
    assert admin.post(base + "/review", json={"id": file_ids[0], "approve": True}, headers=ah).status_code == 409
    assert admin.post("/customer-service/md/quality/cases/add", json={
        "documentKey": document_key, "question": "Test therapy fact", "kind": "fact",
        "evidenceTerms": ["Test therapy fact"], "answerTerms": ["VERSION_"], "critical": True,
    }, headers=ah).status_code == 200
    first_eval = complete_quality_job(admin, ah, file_ids[0])
    assert first_eval.status_code == 200 and first_eval.json()["data"]["passed"], first_eval.text
    pending = admin.post("/customer-service/md/list", json={"reviewStatus": "pending_review"}, headers=ah).json()["data"]
    assert any(item["id"] == file_ids[0] for item in pending)
    assert editor.post(base + "/review", json={"id": file_ids[0], "approve": True}, headers=eh).status_code == 403
    assert admin.post(base + "/review", json={"id": file_ids[0], "approve": True}, headers=ah).status_code == 200
    assert admin.post("/customer-service/md/grants", json={"id": file_ids[0], "userIds": [viewer_info["id"]]}, headers=ah).status_code == 200

    async def embed(values):
        return [[1.] + [0.] * (settings.embedding_dimensions - 1) for _ in values]
    async def answer(messages, model, temperature):
        system = messages[0]["content"]
        assert ("VERSION_1_ONLY" in system) != ("VERSION_2_ONLY" in system)
        yield "VERSION_1_ONLY" if "VERSION_1_ONLY" in system else "VERSION_2_ONLY"
    monkeypatch.setattr(knowledge, "embed_texts", embed)
    monkeypatch.setattr(knowledge, "select_relevant_context", lambda question, rows: [row.content for row in rows])
    monkeypatch.setattr(main, "chat_chunks", answer)

    def ask(expected, excluded):
        selected_id = viewer.get("/customer-service/md/available").json()["data"][0]["id"]
        response = viewer.post("/customer-service/completion", json={"message": "Test therapy fact", "fileId": selected_id}, headers=vh)
        assert response.status_code == 200, response.text
        assert expected in response.text and excluded not in response.text
        assert f'"version": {1 if expected == "VERSION_1_ONLY" else 2}' in response.text
        available = viewer.get("/customer-service/md/available").json()["data"]
        assert len(available) == 1 and available[0]["documentKey"] == document_key
        return response

    version_one = ask("VERSION_1_ONLY", "VERSION_2_ONLY")
    version_one_id = next(json.loads(line[6:])["id"] for line in version_one.text.splitlines()
                          if line.startswith("data: ") and '"id"' in line)
    registered = editor.post(base + "/register", json={"id": file_ids[1], "baseId": file_ids[0],
                    "revisionLabel": "2026 revision", "sourceUrl": "https://example.org/manual"}, headers=eh)
    assert registered.status_code == 200, registered.text
    assert registered.json()["data"]["version"] == 2
    ask("VERSION_1_ONLY", "VERSION_2_ONLY")
    assert editor.post(base + "/submit", json={"id": file_ids[1]}, headers=eh).status_code == 200
    second_eval = complete_quality_job(admin, ah, file_ids[1])
    assert second_eval.status_code == 200 and second_eval.json()["data"]["passed"], second_eval.text
    other, oh, _ = roles["other"]
    assert other.post(base + "/history", json={"documentKey": document_key}, headers=oh).status_code == 404
    assert admin.post(base + "/review", json={"id": file_ids[1], "approve": False}, headers=ah).status_code == 422
    assert admin.post(base + "/review", json={"id": file_ids[1], "approve": False, "note": "核对来源"}, headers=ah).status_code == 200
    ask("VERSION_1_ONLY", "VERSION_2_ONLY")
    assert editor.post(base + "/submit", json={"id": file_ids[1]}, headers=eh).status_code == 200
    assert admin.post(base + "/review", json={"id": file_ids[1], "approve": True}, headers=ah).status_code == 409
    second_eval = complete_quality_job(admin, ah, file_ids[1])
    assert second_eval.status_code == 200 and second_eval.json()["data"]["passed"], second_eval.text
    # Approve between two subquestion SQL statements. One answer must still use
    # the snapshot from its first query, never a mixture of v1/v2 evidence.
    switched = False
    def switch_between_queries(question, rows):
        nonlocal switched
        if not switched:
            switched = True
            result = admin.post(base + "/review", json={"id": file_ids[1], "approve": True}, headers=ah)
            assert result.status_code == 200, result.text
        return [row.content for row in rows]
    monkeypatch.setattr(knowledge, "select_relevant_context", switch_between_queries)
    during = viewer.post("/customer-service/completion", json={
        "message": "Test therapy fact? Another therapy fact?", "fileId": file_ids[0]}, headers=vh)
    assert switched and during.status_code == 200
    assert "VERSION_1_ONLY" in during.text and "VERSION_2_ONLY" not in during.text
    monkeypatch.setattr(knowledge, "select_relevant_context", lambda question, rows: [row.content for row in rows])
    ask("VERSION_2_ONLY", "VERSION_1_ONLY")
    assert viewer.get(f"/customer-service/conversations/{version_one_id}").status_code == 404
    assert viewer.post("/customer-service/completion", json={
        "message": "继续问旧版？", "fileId": file_ids[0], "conversationId": version_one_id},
        headers=vh).status_code == 404
    stale = viewer.post("/customer-service/completion", json={
        "message": "Test therapy fact", "fileId": file_ids[0]}, headers=vh)
    assert stale.status_code == 404
    history = editor.post(base + "/history", json={"documentKey": document_key}, headers=eh).json()["data"]
    assert [entry["reviewStatus"] for entry in history] == ["approved", "superseded"]
    assert history[0]["sourceUrl"].startswith("https://example.org/manual")
    assert editor.post("/customer-service/md/reindex", json={"id": file_ids[0]}, headers=eh).json()["success"] is False
    assert editor.post("/customer-service/md/delete", json={"id": file_ids[0]}, headers=eh).json()["success"] is False
    assert admin.post(base + "/rollback", json={"id": file_ids[0], "reason": "回归测试"}, headers=ah).status_code == 200
    ask("VERSION_1_ONLY", "VERSION_2_ONLY")
    with db_session() as db:
        rows = db.scalars(select(MarkdownFile).where(MarkdownFile.document_key == document_key)).all()
        assert sum(row.is_published for row in rows) == 1
        assert {row.version_number for row in rows} == {1, 2}


def test_version_approval_requires_independent_reviewer(roles):
    admin, ah, admin_info = roles["admin"]
    with db_session() as db:
        row = MarkdownFile(original_file_name="self-review.md", new_file_name="self-review.md",
                           file_path="test-only", file_size=1, status=2, owner_id=admin_info["id"],
                           is_published=False, create_time=datetime.now(), update_time=datetime.now())
        db.add(row)
        db.flush()
        file_id = row.id
    base = "/customer-service/md/versions"
    assert admin.post(base + "/register", json={"id": file_id, "revisionLabel": "v1"}, headers=ah).status_code == 200
    assert admin.post(base + "/submit", json={"id": file_id}, headers=ah).status_code == 200
    rejected = admin.post(base + "/review", json={"id": file_id, "approve": True}, headers=ah)
    assert rejected.status_code == 409
    with db_session() as db:
        row = db.get(MarkdownFile, file_id)
        assert not row.is_published and row.review_status == "pending_review"


def test_release_quality_blocks_failed_and_stale_reports(roles, monkeypatch):
    admin, ah, _ = roles["admin"]
    editor, eh, info = roles["editor"]
    viewer, vh, _ = roles["viewer"]
    with db_session() as db:
        row = MarkdownFile(original_file_name="gate.md", new_file_name="gate.md",
                           file_path="test-only", file_size=1, status=2, owner_id=info["id"],
                           content_sha256="a" * 64, is_published=False,
                           create_time=datetime.now(), update_time=datetime.now())
        db.add(row); db.flush(); file_id = row.id
        db.execute(text("""INSERT INTO t_vector_store(id,content,metadata,embedding)
            VALUES(:id,:content,CAST(:metadata AS json),CAST(:vector AS vector))"""), {
            "id": uuid4(), "content": "## Evidence anchor\nEXPECTED",
            "metadata": json.dumps({"mdStorageId": file_id, "originalFileName": "gate.md"}),
            "vector": vector_literal([1.] + [0.] * (settings.embedding_dimensions - 1))})
    base = "/customer-service/md/versions"
    registered = editor.post(base + "/register", json={"id": file_id, "revisionLabel": "gate"}, headers=eh)
    key = registered.json()["data"]["documentKey"]
    assert editor.post(base + "/submit", json={"id": file_id}, headers=eh).status_code == 200
    label = {"documentKey": key, "question": "Evidence anchor?", "kind": "fact",
             "evidenceTerms": ["Evidence anchor"], "answerTerms": ["EXPECTED"]}
    assert viewer.post("/customer-service/md/quality/cases/add", json=label, headers=vh).status_code == 403
    assert viewer.post("/customer-service/md/quality/run", json={"id": file_id}, headers=vh).status_code == 403
    assert admin.post("/customer-service/md/quality/cases/add", json=label, headers=ah).status_code == 200
    monkeypatch.setattr(release_quality, "require_llm", lambda embedding=False: None)
    async def embed(values): return [[1.] + [0.] * (settings.embedding_dimensions - 1) for _ in values]
    monkeypatch.setattr(knowledge, "embed_texts", embed)
    monkeypatch.setattr(knowledge, "select_relevant_context", lambda question, rows: [row.content for row in rows])
    answer = {"text": "wrong"}
    async def chunks(messages, model, temperature): yield answer["text"]
    monkeypatch.setattr(release_quality, "chat_chunks", chunks)
    failed = complete_quality_job(admin, ah, file_id)
    assert failed.status_code == 200 and not failed.json()["data"]["passed"]
    assert admin.post(base + "/review", json={"id": file_id, "approve": True}, headers=ah).status_code == 409
    answer["text"] = "EXPECTED"
    passed = complete_quality_job(admin, ah, file_id)
    assert passed.status_code == 200 and passed.json()["data"]["passed"]
    added = admin.post("/customer-service/md/quality/cases/add", json={**label, "question": "Another anchor?"}, headers=ah)
    assert added.status_code == 200
    latest = admin.post("/customer-service/md/quality/latest", json={"id": file_id}, headers=ah)
    assert latest.json()["data"]["current"] is False
    assert admin.post(base + "/review", json={"id": file_id, "approve": True}, headers=ah).status_code == 409
    assert admin.post("/customer-service/md/quality/cases/delete", json={"id": added.json()["data"]["id"]}, headers=ah).status_code == 200
    assert complete_quality_job(admin, ah, file_id).json()["data"]["passed"]
    assert admin.post(base + "/review", json={"id": file_id, "approve": True}, headers=ah).status_code == 200


def test_release_quality_resumes_after_lost_lease_without_repeating_completed_cases(roles, monkeypatch):
    admin, ah, _ = roles["admin"]
    editor, eh, info = roles["editor"]
    with db_session() as db:
        row = MarkdownFile(original_file_name="resume.md", new_file_name="resume.md",
                           file_path="test-only", file_size=1, status=2, owner_id=info["id"],
                           content_sha256="b" * 64, is_published=False,
                           create_time=datetime.now(), update_time=datetime.now())
        db.add(row); db.flush(); file_id = row.id
        db.execute(text("""INSERT INTO t_vector_store(id,content,metadata,embedding)
            VALUES(:id,:content,CAST(:metadata AS json),CAST(:vector AS vector))"""), {
            "id": uuid4(), "content": "## Resume anchor\nEXPECTED",
            "metadata": json.dumps({"mdStorageId": file_id, "originalFileName": "resume.md"}),
            "vector": vector_literal([1.] + [0.] * (settings.embedding_dimensions - 1))})
    base = "/customer-service/md/versions"
    key = editor.post(base + "/register", json={"id": file_id, "revisionLabel": "resume"}, headers=eh).json()["data"]["documentKey"]
    assert editor.post(base + "/submit", json={"id": file_id}, headers=eh).status_code == 200
    for question in ("First anchor?", "Second anchor?"):
        assert admin.post("/customer-service/md/quality/cases/add", json={
            "documentKey": key, "question": question, "kind": "fact",
            "evidenceTerms": ["Resume anchor"], "answerTerms": ["EXPECTED"],
        }, headers=ah).status_code == 200
    monkeypatch.setattr(release_quality, "require_llm", lambda embedding=False: None)
    async def embed(values): return [[1.] + [0.] * (settings.embedding_dimensions - 1) for _ in values]
    async def chunks(messages, model, temperature): yield "EXPECTED"
    monkeypatch.setattr(knowledge, "embed_texts", embed)
    monkeypatch.setattr(knowledge, "select_relevant_context", lambda question, rows: [row.content for row in rows])
    monkeypatch.setattr(release_quality, "chat_chunks", chunks)
    path = "/customer-service/md/quality/run"
    first = admin.post(path, json={"id": file_id}, headers=ah).json()["data"]
    second = admin.post(path, json={"id": file_id}, headers=ah).json()["data"]
    assert first["jobId"] == second["jobId"]
    job = quality_queue.claim_quality("crashing-worker", candidate_id=file_id)
    assert quality_queue.claim_quality("other-worker", candidate_id=file_id) is None
    original_one_case = quality_queue._one_case
    calls = []
    crashed = False
    def once(job_data, case, actor):
        nonlocal crashed
        calls.append(case.id)
        if len(calls) == 2 and not crashed:
            crashed = True
            raise RuntimeError("simulated process failure")
        return original_one_case(job_data, case, actor)
    monkeypatch.setattr(quality_queue, "_one_case", once)
    with pytest.raises(RuntimeError):
        quality_queue.process_quality(job)
    status = admin.post("/customer-service/md/quality/status", json={"id": file_id}, headers=ah).json()["data"]
    assert status["cursor"] == 1 and status["status"] == "running"
    with db_session() as db:
        db.execute(text("UPDATE t_release_eval_job SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"),
                   {"id": job["id"]})
    assert quality_queue.recover_expired_quality() == 1
    with pytest.raises(quality_queue.LeaseLost):
        quality_queue.process_quality(job)
    with db_session() as db:
        db.execute(text("UPDATE t_release_eval_job SET available_at=clock_timestamp()-interval '1 second' WHERE id=:id"),
                   {"id": job["id"]})
    retried = quality_queue.claim_quality("replacement-worker", candidate_id=file_id)
    assert retried and retried["attempts"] == 2
    quality_queue.process_quality(retried)
    assert calls == [calls[0], calls[1], calls[1]]
    final = admin.post("/customer-service/md/quality/status", json={"id": file_id}, headers=ah).json()["data"]
    assert final["status"] == "succeeded" and final["cursor"] == 2
    report = admin.post("/customer-service/md/quality/latest", json={"id": file_id}, headers=ah).json()["data"]
    assert report["passed"] and len(report["cases"]) == 2


def test_password_logout_expiry_and_disabled_users(roles):
    viewer, vh, info = roles["viewer"]
    new_password = "Updated-test-password-2026"
    assert viewer.post("/auth/password", json={"oldPassword": PASSWORD, "newPassword": new_password}, headers=vh).status_code == 200
    assert viewer.get("/auth/me").status_code == 401
    assert viewer.post("/auth/login", json={"username": info["username"], "password": PASSWORD}).status_code == 401
    login = viewer.post("/auth/login", json={"username": info["username"], "password": new_password})
    assert login.status_code == 200
    assert viewer.post("/auth/logout", json={}, headers={"X-CSRF-Token": login.json()["data"]["csrfToken"]}).status_code == 200
    assert viewer.get("/auth/me").status_code == 401
    viewer.post("/auth/login", json={"username": info["username"], "password": new_password})
    with db_session() as db:
        session = db.get(AuthSession, token_hash(viewer.cookies.get(COOKIE)))
        session.expires_at = datetime.now() - timedelta(seconds=1)
    assert viewer.get("/auth/me").status_code == 401
    admin, ah, _ = roles["admin"]
    assert admin.post("/admin/users/update", json={"id": info["id"], "role": "viewer", "isActive": False}, headers=ah).status_code == 200
    assert viewer.post("/auth/login", json={"username": info["username"], "password": new_password}).status_code == 401
