"""Durable, file-scoped conversations for grounded follow-up questions.

Only previous user questions may help resolve a reference. Assistant replies are
stored for display, never fed back into retrieval or used as knowledge evidence.
"""

from datetime import datetime, timedelta, timezone
import copy
import hashlib
from functools import lru_cache
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import delete, or_, select, text

from app.access import readable_scope
from app.db import ChatConversation, ChatTurn, MarkdownFile, db_session


HISTORY_QUESTIONS = 4
DISPLAY_TURNS = 50
RETENTION_DAYS = 30
LEASE_SECONDS = 300
FOLLOWUP = re.compile(r"^(?:那|那么|它|该|这个|那个|上述|前述|其中|还|继续|其)")
MODEL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-–][A-Za-z0-9]+)+")


@lru_cache(maxsize=1)
def evidence_code_revision():
    root = Path(__file__).parent
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in ("knowledge.py", "pdf_rerank.py", "answer_prompt.py")}


def capture_answer_evidence(file_id: int, documents: list[dict], retrieval_question: str) -> dict:
    from app.config import settings
    with db_session() as db:
        row = db.get(MarkdownFile, file_id)
        if not row:
            raise HTTPException(404, "资料不存在")
        return {"schemaVersion": 1, "capturedAt": utc_now().isoformat()+"Z",
            "file": {"id": row.id, "fileName": row.original_file_name,
                "documentKey": str(row.document_key) if row.document_key else None,
                "version": row.version_number, "sourceSha256": row.content_sha256,
                "indexConfig": copy.deepcopy(row.index_config), "pdfQaRevision": row.pdf_qa_revision},
            "retrievalQuestion": retrieval_question, "model": settings.customer_model, "temperature": 0,
            "embeddingModel": settings.embedding_model, "codeRevision": evidence_code_revision(),
            "documents": copy.deepcopy(documents)}


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def contextualize_question(question: str, prior_questions: list[str]) -> tuple[str, str | None]:
    """Use a bounded prior *user question* only for referent resolution."""
    if not prior_questions or MODEL_NAME.search(question) or not (FOLLOWUP.search(question) or len(question) <= 16):
        return question, None
    previous = prior_questions[-1]
    anchor = next((match.group() for old in reversed(prior_questions)
                   if (match := MODEL_NAME.search(old))), None)
    if anchor:
        return f"{anchor} {question}", previous
    # Without a reliable model identifier, retain the last question as a
    # disambiguation hint. Retrieval is still restricted to the selected file.
    return f"{previous[:160].rstrip('？?。')}；本轮追问：{question}", previous


def _readable_file(db, user_id: int, role: str, file_id: int) -> bool:
    return db.scalar(select(MarkdownFile.id).where(
        MarkdownFile.id == file_id, MarkdownFile.status == 2,
        readable_scope(SimpleNamespace(id=user_id, role=role))).limit(1)) is not None


def open_conversation(user_id: int, role: str, file_id: int,
                      conversation_id: UUID | None, request_id: UUID) -> tuple[UUID, list[str]]:
    """Atomically claim the session and read only recent user questions."""
    now = utc_now()
    with db_session() as db:
        if not _readable_file(db, user_id, role, file_id):
            raise HTTPException(404, "资料不存在、未发布或无查询权限，请刷新可查询资料")
        if conversation_id is None:
            row = ChatConversation(id=uuid4(), user_id=user_id, file_id=file_id,
                                   created_at=now, updated_at=now,
                                   expires_at=now + timedelta(days=RETENTION_DAYS))
            db.add(row)
            db.flush()
        else:
            row = db.scalar(select(ChatConversation).where(
                ChatConversation.id == conversation_id, ChatConversation.user_id == user_id).with_for_update())
            if row is None:
                raise HTTPException(404, "会话不存在或无访问权限")
            if row.file_id != file_id:
                raise HTTPException(409, "会话属于另一份资料，请新建会话")
            if row.expires_at <= now:
                raise HTTPException(410, "会话已过期，请新建会话")
            if row.active_request_id is not None and row.lease_until and row.lease_until > now:
                raise HTTPException(409, "该会话已有回答正在生成，请稍后再试")
        questions = db.scalars(select(ChatTurn.question).where(ChatTurn.conversation_id == row.id)
                               .order_by(ChatTurn.id.desc()).limit(HISTORY_QUESTIONS)).all()
        row.active_request_id = request_id
        row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        row.updated_at = now
        return row.id, list(reversed(questions))


def renew_conversation(conversation_id: UUID, request_id: UUID) -> bool:
    now = utc_now()
    with db_session() as db:
        return db.execute(text("""UPDATE t_chat_conversation
            SET lease_until=:new_lease WHERE id=:id AND active_request_id=:request_id
              AND lease_until>:now RETURNING id"""), {
            "id": conversation_id, "request_id": request_id, "now": now,
            "new_lease": now + timedelta(seconds=LEASE_SECONDS)}).scalar_one_or_none() is not None


def finish_conversation(conversation_id: UUID, request_id: UUID, *, question: str | None = None,
                        answer: str | None = None, sources: list[dict] | None = None,
                        evidence_snapshot: dict | None = None) -> bool:
    """Persist a complete turn once; a superseded worker cannot append a stale answer."""
    now = utc_now()
    with db_session() as db:
        row = db.scalar(select(ChatConversation).where(ChatConversation.id == conversation_id).with_for_update())
        if row is None or row.active_request_id != request_id:
            return False
        if row.lease_until is None or row.lease_until <= now:
            row.active_request_id = None
            row.lease_until = None
            return False
        if answer is not None:
            if question is None:
                raise ValueError("保存回答时必须提供用户问题")
            db.add(ChatTurn(conversation_id=conversation_id, request_id=request_id,
                            question=question, answer=answer, sources=sources or [],
                            evidence_snapshot=evidence_snapshot, created_at=now))
            row.expires_at = now + timedelta(days=RETENTION_DAYS)
        row.active_request_id = None
        row.lease_until = None
        row.updated_at = now
        return True


def get_conversation(conversation_id: UUID, user_id: int, role: str) -> dict:
    with db_session() as db:
        row = db.scalar(select(ChatConversation).where(
            ChatConversation.id == conversation_id, ChatConversation.user_id == user_id))
        if row is None:
            raise HTTPException(404, "会话不存在或无访问权限")
        if row.expires_at <= utc_now():
            raise HTTPException(410, "会话已过期，请新建会话")
        if not _readable_file(db, user_id, role, row.file_id):
            raise HTTPException(404, "资料不存在、未发布或无查询权限，请刷新可查询资料")
        turns = db.scalars(select(ChatTurn).where(ChatTurn.conversation_id == row.id)
                           .order_by(ChatTurn.id.desc()).limit(DISPLAY_TURNS)).all()
        return {"id": str(row.id), "fileId": row.file_id, "expiresAt": row.expires_at.isoformat() + "Z",
                "turns": [{"requestId": str(turn.request_id), "question": turn.question, "answer": turn.answer, "sources": turn.sources or []}
                          for turn in reversed(turns)]}


def prune_expired_conversations() -> int:
    """Called by the monitor process; never remove an actively leased turn."""
    now = utc_now()
    with db_session() as db:
        result = db.execute(delete(ChatConversation).where(
            ChatConversation.expires_at <= now,
            or_(ChatConversation.active_request_id.is_(None), ChatConversation.lease_until <= now)))
        return result.rowcount
