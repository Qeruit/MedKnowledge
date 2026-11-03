"""Standalone medical-document RAG demo API.

The API deliberately has no general-chat routes and uses an isolated database.
It is a document assistant, not a clinical decision support system.
"""

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import logging
from pathlib import Path
import re
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import delete, func, select, text
from starlette.background import BackgroundTask
from app.streaming import DisconnectAwareStreamingResponse

from app.config import settings
from app.contracts import CustomerCompletionRequest, MarkdownIdRequest, MarkdownPageRequest, UpdateMarkdownRequest, fail, ok, page
from app.db import FileGrant, MarkdownFile, User, db_session
from app.auth import router as auth_router, current_user, editor_required, admin_required
from app.access import management_scope, readable_scope, require_file_manager
from app.audit import audit
from pydantic import BaseModel, Field
from app.knowledge import find_context
from app.task_queue import enqueue
from app.llm import chat_chunks, require_llm
from app.resumable_upload import UploadInitRequest, complete_upload, init_upload, put_chunk, upload_status
from app.documents import DocumentReadError, document_format, require_format_available, validate_document
from app.storage_guard import storage_write_guard
from app.observability import OpsMiddleware
from app.operations import router as ops_router
from app.versioning import router as version_router
from app.release_quality import router as quality_router
from app.pdf_quality import router as pdf_quality_router
from app.precheck_agent import router as precheck_router
from app.answer_prompt import answer_messages, evidence_reply, NO_EVIDENCE_REPLY
from app.chat_limits import begin_request, capped_delta, finish_request, message_tokens, reserve_prompt, renew_request, token_count
from app.chat_history import (contextualize_question, finish_conversation, get_conversation,
                              open_conversation, renew_conversation, capture_answer_evidence)
from app.feedback import router as feedback_router

logger = logging.getLogger(__name__)


app = FastAPI(title="医疗资料知识库问答与管理系统", version="1.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type", "X-CSRF-Token", "X-Chunk-Sha256"],
)


app.include_router(auth_router)
app.include_router(ops_router)
app.include_router(version_router)
app.include_router(quality_router)
app.include_router(pdf_quality_router)
app.include_router(precheck_router)
app.include_router(feedback_router)
app.add_middleware(OpsMiddleware)


@app.exception_handler(DocumentReadError)
async def document_error_handler(request: Request, exc: DocumentReadError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


def sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_data(value: str) -> str:
    return f"data: {json.dumps({'v': value}, ensure_ascii=False)}\n\n"


def sse_close() -> str:
    return sse_event("close", {})


def iso(value):
    return value.isoformat(timespec="seconds") if value else None


def format_size(byte_count: int) -> str:
    size = float(byte_count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return "?"


@app.get("/health")
def health():
    return {"status": "ok", "databaseConfigured": bool(settings.database_url),
            "modelConfigured": bool(settings.llm_base_url and settings.llm_api_key)}


@app.get("/ready")
def ready():
    try:
        with db_session() as db:
            db.execute(text("SELECT document_key, review_status, embedding_model FROM t_ai_customer_service_md_storage LIMIT 1"))
            db.execute(text("SELECT id FROM t_precheck_task LIMIT 1"))
            db.execute(text("SELECT 1 FROM t_index_job LIMIT 1"))
            db.execute(text("SELECT 1 FROM t_pdf_page_audit LIMIT 1"))
            db.execute(text("SELECT 1 FROM t_chat_request LIMIT 1"))
            db.execute(text("SELECT 1 FROM t_chat_conversation LIMIT 1"))
            db.execute(text("SELECT 1 FROM t_chat_turn LIMIT 1"))
            db.execute(text("SELECT evidence_snapshot FROM t_chat_turn LIMIT 1"))
            db.execute(text("SELECT 1 FROM t_answer_feedback LIMIT 1"))
            if not db.scalar(select(User.id).where(User.role == "admin", User.is_active.is_(True)).limit(1)):
                raise RuntimeError("需要初始化管理员账号")
        require_llm(embedding=True)
        return {"status": "ready"}
    except Exception as exc:
        logger.warning("Readiness check failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="数据库、表结构或模型配置尚未就绪") from exc


def source_cards(documents: list[dict]) -> list[dict]:
    cards = []
    for document in documents:
        content = document["content"]
        metadata = document.get("metadata") or {}
        match = re.search(r"来源：([^\n]+)", content)
        cards.append({
            "heading": (metadata.get("sectionTitle") or content.splitlines()[0].lstrip("# ").strip())[:160],
            "citation": ("；".join(metadata.get("citations") or []) or
                         (match.group(1).strip() if match else "未提供页码"))[:260],
            "sourceUrl": metadata.get("sourceUrl"),
            "fileName": metadata.get("originalFileName"),
            "documentKey": metadata.get("documentKey"),
            "version": metadata.get("documentVersion"),
        })
    return cards


@app.post("/customer-service/completion")
async def customer_completion(payload: CustomerCompletionRequest, user: User = Depends(current_user)):
    question = payload.message.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    try:
        require_llm(embedding=True)
    except RuntimeError as exc:
        raise HTTPException(503, "模型服务尚未配置") from exc
    request_id = await asyncio.to_thread(begin_request, user.id, token_count(question))
    try:
        conversation_id, prior_questions = await asyncio.to_thread(
            open_conversation, user.id, user.role, payload.fileId, payload.conversationId, request_id)
    except HTTPException:
        await asyncio.to_thread(finish_request, request_id, outcome="rejected")
        raise
    except Exception as exc:
        await asyncio.to_thread(finish_request, request_id, outcome="error")
        logger.exception("Conversation opening failed")
        raise HTTPException(status_code=503, detail="会话暂不可用") from exc
    retrieval_question, reference_question = contextualize_question(question, prior_questions)
    try:
        documents = await find_context(retrieval_question, user_id=user.id, role=user.role, file_id=payload.fileId)
        evidence_snapshot = await asyncio.to_thread(capture_answer_evidence, payload.fileId, documents, retrieval_question)
    except HTTPException:
        try:
            await asyncio.to_thread(finish_conversation, conversation_id, request_id)
        finally:
            await asyncio.to_thread(finish_request, request_id, outcome="rejected")
        raise
    except Exception as exc:
        try:
            await asyncio.to_thread(finish_conversation, conversation_id, request_id)
        finally:
            await asyncio.to_thread(finish_request, request_id, outcome="error")
        logger.exception("Knowledge retrieval failed")
        raise HTTPException(status_code=503, detail="知识库暂不可用") from exc

    fallback = evidence_reply(question, documents)
    evidence_snapshot["generationMode"] = "deterministic_fallback" if fallback else "llm"
    if fallback:
        try:
            saved = await asyncio.to_thread(finish_conversation, conversation_id, request_id,
                                            question=question, answer=fallback, sources=source_cards(documents),
                                            evidence_snapshot=evidence_snapshot)
            if not saved:
                raise HTTPException(409, "会话已被其他请求接管，请重新提问")
        except Exception:
            await asyncio.to_thread(finish_request, request_id, outcome="error")
            raise
        await asyncio.to_thread(finish_request, request_id)
        async def no_context():
            yield sse_event("conversation", {"id": str(conversation_id), "fileId": payload.fileId, "requestId": str(request_id)})
            if documents:
                yield sse_event("sources", {"items": source_cards(documents)})
            yield sse_data(fallback)
            yield sse_close()
        return StreamingResponse(no_context(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    messages = answer_messages(question, documents, reference_question=reference_question)
    try:
        await asyncio.to_thread(reserve_prompt, request_id, message_tokens(messages))
    except Exception:
        try:
            await asyncio.to_thread(finish_conversation, conversation_id, request_id)
        finally:
            await asyncio.to_thread(finish_request, request_id, outcome="rejected")
        raise

    stream_started = False

    async def release_unstarted_stream():
        if not stream_started:
            try:
                await asyncio.to_thread(finish_conversation, conversation_id, request_id)
            finally:
                await asyncio.to_thread(finish_request, request_id, outcome="cancelled")

    async def stream():
        nonlocal stream_started
        stream_started = True
        response_text = ""
        outcome = "success"
        persistence_error = False
        lease_lost = asyncio.Event()
        async def keep_lease():
            while True:
                await asyncio.sleep(30)
                try:
                    renewed = (await asyncio.to_thread(renew_request, request_id)
                               and await asyncio.to_thread(renew_conversation, conversation_id, request_id))
                except Exception:
                    logger.exception("Chat request lease renewal failed")
                    lease_lost.set()
                    return
                if not renewed:
                    lease_lost.set()
                    return
        heartbeat = asyncio.create_task(keep_lease())
        try:
            yield sse_event("conversation", {"id": str(conversation_id), "fileId": payload.fileId, "requestId": str(request_id)})
            yield sse_event("sources", {"items": source_cards(documents)})
            async for chunk in chat_chunks(messages, settings.customer_model, 0):
                if lease_lost.is_set():
                    raise RuntimeError("问答名额租约已失效")
                response_text, delta, capped = capped_delta(response_text, chunk)
                if delta:
                    yield sse_data(delta)
                if capped:
                    break
        except (asyncio.CancelledError, GeneratorExit):
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            logger.exception("Answer generation failed")
            yield sse_event("error", {"message": "回答生成失败，请稍后重试"})
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                saved = await asyncio.shield(asyncio.to_thread(
                    finish_conversation, conversation_id, request_id,
                    question=question if outcome == "success" else None,
                    answer=response_text if outcome == "success" else None,
                    sources=source_cards(documents) if outcome == "success" else None,
                    evidence_snapshot=evidence_snapshot if outcome == "success" else None))
                if outcome == "success" and not saved:
                    outcome = "error"
                    persistence_error = True
            except Exception:
                logger.exception("Conversation turn persistence failed")
                outcome = "error"
                persistence_error = True
            await asyncio.shield(asyncio.to_thread(
                finish_request, request_id, output_tokens=token_count(response_text), outcome=outcome))
        if outcome == "success":
            yield sse_close()
        elif persistence_error:
            yield sse_event("error", {"message": "回答保存失败，请新建会话后重试"})

    return DisconnectAwareStreamingResponse(stream(), media_type="text/event-stream",
                             background=BackgroundTask(release_unstarted_stream),
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/customer-service/conversations/{conversation_id}")
def conversation_detail(conversation_id: UUID, user: User = Depends(current_user)):
    return ok(get_conversation(conversation_id, user.id, user.role))


@app.post("/customer-service/md/list")
def markdown_list(payload: MarkdownPageRequest, user: User = Depends(editor_required)):
    query = select(MarkdownFile).where(management_scope(user))
    if payload.fileName and payload.fileName.strip():
        query = query.where(MarkdownFile.original_file_name.contains(payload.fileName.strip(), autoescape=True))
    if payload.reviewStatus:
        query = query.where(MarkdownFile.review_status == payload.reviewStatus)
    if payload.startDate:
        query = query.where(MarkdownFile.create_time >= datetime.combine(payload.startDate, datetime.min.time()))
    if payload.endDate:
        query = query.where(MarkdownFile.create_time < datetime.combine(payload.endDate + timedelta(days=1), datetime.min.time()))
    with db_session() as db:
        total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = db.scalars(query.order_by(MarkdownFile.create_time.desc(), MarkdownFile.id.desc())
                          .offset((payload.current - 1) * payload.size).limit(payload.size)).all()
        grants = {}
        if user.role == "admin" and rows:
            for grant in db.scalars(select(FileGrant).where(FileGrant.file_id.in_([row.id for row in rows]))).all():
                grants.setdefault(grant.file_id, []).append(grant.user_id)
        names = {row.id: row.username for row in db.scalars(select(User)).all()}
        jobs = {}
        if rows:
            jobs = {job.file_id: job for job in db.execute(text("""SELECT DISTINCT ON (file_id)
                file_id,status,attempts,max_attempts,last_error,available_at,lease_until
                FROM t_index_job WHERE file_id=ANY(:ids) ORDER BY file_id,create_time DESC,id DESC"""),
                {"ids": [row.id for row in rows]}).all()}
        data = [{
            "id": row.id, "originalFileName": row.original_file_name,
            "fileSize": format_size(row.file_size), "status": row.status,
            "createTime": iso(row.create_time), "updateTime": iso(row.update_time),
            "remark": row.remark,
            "fileFormat": document_format(row.original_file_name),
            "processingError": row.processing_error,
            "processingWarnings": row.processing_warnings or [],
            "ownerId": row.owner_id, "ownerName": names.get(row.owner_id),
            "isPublished": row.is_published, "grantedUserIds": grants.get(row.id, []) if user.role == "admin" else [],
            "documentKey": str(row.document_key) if row.document_key else None,
            "version": row.version_number, "reviewStatus": row.review_status,
            "revisionLabel": row.revision_label, "sourceUrl": row.source_url,
            "sha256": row.content_sha256, "submittedAt": iso(row.submitted_at),
            "reviewedAt": iso(row.reviewed_at), "reviewerId": row.reviewer_id,
            "reviewNote": row.review_note,
            "embeddingModel": row.embedding_model, "indexConfig": row.index_config,
            "task": ({"status": jobs[row.id].status, "attempts": jobs[row.id].attempts,
                      "maxAttempts": jobs[row.id].max_attempts, "lastError": jobs[row.id].last_error,
                      "availableAt": iso(jobs[row.id].available_at), "leaseUntil": iso(jobs[row.id].lease_until)}
                     if row.id in jobs else None),
        } for row in rows]
    return page(data, total, payload.current, payload.size)


@app.get("/customer-service/md/stats")
def markdown_stats(user: User = Depends(editor_required)):
    with db_session() as db:
        counts = db.execute(select(MarkdownFile.status, func.count()).where(management_scope(user)).group_by(MarkdownFile.status)).all()
        vectors = db.execute(text("""SELECT count(*) FROM t_vector_store v
            JOIN t_ai_customer_service_md_storage f ON v.metadata->>'mdStorageId'=f.id::text
            WHERE :admin OR f.owner_id=:uid"""), {"admin": user.role == "admin", "uid": user.id}).scalar_one()
        jobs = db.execute(text("""SELECT j.status,count(*) FROM t_index_job j
            JOIN t_ai_customer_service_md_storage f ON f.id=j.file_id
            WHERE :admin OR f.owner_id=:uid GROUP BY j.status"""),
            {"admin": user.role == "admin", "uid": user.id}).all()
        workers = db.scalar(text("""SELECT count(*) FROM t_worker_heartbeat
            WHERE last_seen > clock_timestamp() - :ttl * interval '1 second'"""),
            {"ttl": settings.task_lease_seconds * 2})
    return ok({"filesByStatus": {str(status): count for status, count in counts}, "vectorCount": vectors,
               "tasksByStatus": dict(jobs), "workersAlive": workers})


class PublishRequest(MarkdownIdRequest):
    isPublished: bool


class GrantRequest(MarkdownIdRequest):
    userIds: list[int] = Field(default_factory=list, max_length=100)


@app.get("/customer-service/md/available")
def available_files(user: User = Depends(current_user)):
    with db_session() as db:
        rows = db.scalars(select(MarkdownFile).where(readable_scope(user), MarkdownFile.status == 2)
                          .order_by(MarkdownFile.id)).all()
        return ok([{"id": row.id, "fileName": row.original_file_name,
                    "documentKey": str(row.document_key) if row.document_key else None,
                    "version": row.version_number} for row in rows])


@app.post("/customer-service/md/publish",dependencies=[Depends(storage_write_guard)])
def publish_file(payload: PublishRequest, user: User = Depends(editor_required)):
    with db_session() as db:
        row = db.scalar(select(MarkdownFile).where(MarkdownFile.id == payload.id).with_for_update())
        require_file_manager(row, user)
        if row.document_key is not None or payload.isPublished or not row.is_published:
            raise HTTPException(409, "请通过版本登记、送审和管理员审核发布；版本切换请使用回滚接口")
        # Legacy published records may still be withdrawn without destroying data.
        row.is_published = payload.isPublished
        row.update_time = datetime.now()
        audit(db, "publish" if payload.isPublished else "unpublish", row.id, actor=user)
    return ok()


@app.post("/customer-service/md/grants",dependencies=[Depends(storage_write_guard)])
def grant_file(payload: GrantRequest, user: User = Depends(admin_required)):
    ids = set(payload.userIds)
    with db_session() as db:
        row = db.scalar(select(MarkdownFile).where(MarkdownFile.id == payload.id).with_for_update())
        require_file_manager(row, user)
        if row.document_key is not None and not row.is_published:
            raise HTTPException(409, "仅可授权当前已发布版本；版本切换时授权会自动继承")
        existing = set(db.scalars(select(User.id).where(User.id.in_(ids), User.is_active.is_(True))).all())
        if existing != ids:
            raise HTTPException(400, "授权名单中包含不存在或停用的用户")
        db.execute(delete(FileGrant).where(FileGrant.file_id == payload.id))
        for user_id in ids:
            db.add(FileGrant(file_id=payload.id, user_id=user_id))
        audit(db, "grant", row.id, "users:" + ",".join(map(str, sorted(ids))), actor=user)
    return ok()


@app.get("/customer-service/md/audit")
def markdown_audit(user: User = Depends(editor_required)):
    with db_session() as db:
        rows = db.execute(text("""
            SELECT id, action, file_id, detail, create_time, actor_id, actor_name FROM t_admin_audit
            WHERE :admin OR actor_id=:uid
            ORDER BY id DESC LIMIT 50
        """), {"admin": user.role == "admin", "uid": user.id}).all()
    return ok([{"id": row.id, "action": row.action, "fileId": row.file_id,
                "detail": row.detail, "createTime": iso(row.create_time),
                "actorId": row.actor_id, "actorName": row.actor_name or "历史记录（未记录操作者）"} for row in rows])


@app.post("/customer-service/md/upload",dependencies=[Depends(storage_write_guard)])
async def upload_markdown(file: UploadFile | None = File(default=None),
                          user: User = Depends(editor_required)):
    if file is None:
        return fail("上传文件不能为空", "20002")
    original = (file.filename or "").replace("\\", "/").split("/")[-1].strip()
    if len(original) > 200:
        return fail("文件名不能超过 200 字符", "20003")
    require_format_available(original)
    content = await file.read(10 * 1024 * 1024 + 1)
    await file.close()
    if not content or len(content) > 10 * 1024 * 1024:
        return fail("文件不能为空且不能超过 10 MB", "20002")
    storage = settings.md_storage_path
    storage.mkdir(parents=True, exist_ok=True)
    new_name = f"{uuid4()}-{original}"
    target = storage / new_name
    target.write_bytes(content)
    try:
        validate_document(target, original)
    except DocumentReadError as exc:
        target.unlink(missing_ok=True)
        raise exc
    now = datetime.now()
    try:
        with db_session() as db:
            row = MarkdownFile(original_file_name=original, new_file_name=new_name,
                               file_path=str(target), file_size=len(content), status=0,
                               content_sha256=hashlib.sha256(content).hexdigest(),
                               remark=None, create_time=now, update_time=now, owner_id=user.id)
            db.add(row)
            db.flush()
            file_id = row.id
            audit(db, "upload", file_id, original, actor=user)
            enqueue(db, file_id)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return ok({"id": file_id, "status": 0})


@app.get("/customer-service/md/formats")
def supported_formats(user: User = Depends(editor_required)):
    return ok({"extensions": ["md", "markdown", "txt", "pdf", "docx"] +
               (["doc"] if settings.tika_server_url else []),
               "legacyDocConfigured": bool(settings.tika_server_url), "ocrEnabled": False})


@app.post("/customer-service/md/uploads",dependencies=[Depends(storage_write_guard)])
def create_resumable_upload(payload: UploadInitRequest, user: User = Depends(editor_required)):
    return ok(init_upload(payload, user))


@app.get("/customer-service/md/uploads/{upload_id}")
def get_resumable_upload(upload_id: UUID, user: User = Depends(editor_required)):
    return ok(upload_status(upload_id, user))


@app.put("/customer-service/md/uploads/{upload_id}/chunks/{index}",dependencies=[Depends(storage_write_guard)])
async def upload_resumable_chunk(upload_id: UUID, index: int, request: Request,
                                 x_chunk_sha256: str = Header(), user: User = Depends(editor_required)):
    return ok(await put_chunk(upload_id, index, x_chunk_sha256, request, user))


@app.post("/customer-service/md/uploads/{upload_id}/complete",dependencies=[Depends(storage_write_guard)])
def finish_resumable_upload(upload_id: UUID, user: User = Depends(editor_required)):
    result = complete_upload(upload_id, user)
    if not result["instant"] and not result["alreadyCompleted"]:
        with db_session() as db:
            audit(db, "upload", result["fileId"], f"resumable:{upload_id}", actor=user)
    return ok(result)


@app.post("/customer-service/md/update",dependencies=[Depends(storage_write_guard)])
def update_markdown(payload: UpdateMarkdownRequest, user: User = Depends(editor_required)):
    with db_session() as db:
        row = db.get(MarkdownFile, payload.id)
        require_file_manager(row, user)
        row.remark = payload.remark
        row.update_time = datetime.now()
        audit(db, "update", row.id, "remark", actor=user)
    return ok()


@app.post("/customer-service/md/retry",dependencies=[Depends(storage_write_guard)])
def retry_markdown(payload: MarkdownIdRequest, user: User = Depends(editor_required)):
    with db_session() as db:
        row = db.scalar(select(MarkdownFile).where(MarkdownFile.id == payload.id).with_for_update())
        require_file_manager(row, user)
        if row.document_key is not None and row.review_status not in ("draft", "rejected"):
            return fail("已送审版本不能重试，请上传新版本", "20008")
        if row.status != 3:
            return fail("只有失败状态的资料可以重试", "20006")
        if not Path(row.file_path).is_file():
            return fail("原始文件已丢失，无法重试", "20007")
        audit(db, "retry", row.id, actor=user)
        enqueue(db, row.id)
    return ok()


@app.post("/customer-service/md/reindex",dependencies=[Depends(storage_write_guard)])
def reindex_markdown(payload: MarkdownIdRequest, user: User = Depends(editor_required)):
    with db_session() as db:
        row = db.scalar(select(MarkdownFile).where(MarkdownFile.id == payload.id).with_for_update())
        require_file_manager(row, user)
        if row.document_key is not None and row.review_status not in ("draft", "rejected"):
            return fail("已送审或发布的版本不可原地重新索引，请上传新版本", "20008")
        if row.status in (0, 1):
            return fail("处理中资料不能重复索引", "20005")
        if not Path(row.file_path).is_file():
            return fail("原始文件已丢失，无法重新索引", "20007")
        row.status = 0
        row.update_time = datetime.now()
        audit(db, "reindex", row.id, "current splitter configuration", actor=user)
        enqueue(db, row.id)
    return ok({"id": payload.id, "status": 0})


@app.post("/customer-service/md/delete",dependencies=[Depends(storage_write_guard)])
def delete_markdown(payload: MarkdownIdRequest, user: User = Depends(editor_required)):
    with db_session() as db:
        row = db.scalar(select(MarkdownFile).where(MarkdownFile.id == payload.id).with_for_update())
        require_file_manager(row, user)
        if row.document_key is not None:
            return fail("已登记的资料版本需保留用于审计和回滚，不能直接删除", "20008")
        if row.status in (0, 1):
            return fail("处理中资料不能删除", "20005")
        file_path = Path(row.file_path).resolve()
        db.execute(text("DELETE FROM t_vector_store WHERE metadata ->> 'mdStorageId' = :id"),
                   {"id": str(payload.id)})
        db.delete(row)
        audit(db, "delete", payload.id, row.original_file_name, actor=user)
    if file_path.is_relative_to(settings.md_storage_path):
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Database row removed but local file remains: %s", file_path)
    else:
        logger.warning("Legacy path outside managed storage needs manual cleanup: %s", file_path)
    return ok()
