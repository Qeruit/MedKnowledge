from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.config import settings
from uuid import UUID
from sqlalchemy.dialects.postgresql import UUID as PgUUID


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "t_user"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AuthSession(Base):
    __tablename__ = "t_auth_session"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("t_user.id"))
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    create_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class FileGrant(Base):
    __tablename__ = "t_file_grant"
    file_id: Mapped[int] = mapped_column(ForeignKey("t_ai_customer_service_md_storage.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("t_user.id", ondelete="CASCADE"), primary_key=True)


class MarkdownFile(Base):
    __tablename__ = "t_ai_customer_service_md_storage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    original_file_name: Mapped[str] = mapped_column(String(255))
    new_file_name: Mapped[str] = mapped_column(String(300))
    file_path: Mapped[str] = mapped_column(Text)
    file_size: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[int] = mapped_column(Integer)
    remark: Mapped[str | None] = mapped_column(Text)
    create_time: Mapped[datetime] = mapped_column(DateTime)
    update_time: Mapped[datetime] = mapped_column(DateTime)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    processing_error: Mapped[str | None] = mapped_column(Text)
    processing_warnings: Mapped[list] = mapped_column(JSON, default=list)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("t_user.id"))
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    document_key: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    version_number: Mapped[int | None] = mapped_column(Integer)
    review_status: Mapped[str | None] = mapped_column(String(16))
    source_url: Mapped[str | None] = mapped_column(Text)
    revision_label: Mapped[str | None] = mapped_column(String(100))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("t_user.id"))
    review_note: Mapped[str | None] = mapped_column(String(500))
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    index_config: Mapped[dict | None] = mapped_column(JSON)
    pdf_qa_revision: Mapped[int] = mapped_column(Integer, default=0)


class PdfPageAudit(Base):
    __tablename__ = "t_pdf_page_audit"
    file_id: Mapped[int] = mapped_column(ForeignKey("t_ai_customer_service_md_storage.id", ondelete="CASCADE"), primary_key=True)
    page_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_label: Mapped[str | None] = mapped_column(String(64))
    extracted_text: Mapped[str] = mapped_column(Text)
    flags: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(String(500))
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("t_user.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    source_sha256: Mapped[str] = mapped_column(String(64))


class ReleaseEvalCase(Base):
    __tablename__ = "t_release_eval_case"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_key: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True))
    question: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16))
    evidence_terms: Mapped[list] = mapped_column(JSON)
    answer_terms: Mapped[list] = mapped_column(JSON)
    critical: Mapped[bool] = mapped_column(Boolean)
    create_time: Mapped[datetime] = mapped_column(DateTime)


class ReleaseEvalRun(Base):
    __tablename__ = "t_release_eval_run"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("t_ai_customer_service_md_storage.id", ondelete="CASCADE"))
    baseline_id: Mapped[int | None] = mapped_column(BigInteger)
    fingerprint: Mapped[str] = mapped_column(String(64))
    passed: Mapped[bool] = mapped_column(Boolean)
    report: Mapped[dict] = mapped_column(JSON)
    actor_id: Mapped[int] = mapped_column(ForeignKey("t_user.id"))
    create_time: Mapped[datetime] = mapped_column(DateTime)


class ReleaseEvalJob(Base):
    __tablename__ = "t_release_eval_job"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("t_ai_customer_service_md_storage.id", ondelete="CASCADE"))
    baseline_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_id: Mapped[int] = mapped_column(ForeignKey("t_user.id"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    cursor: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer)
    results: Mapped[list] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(Integer)
    claim_token: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime)
    available_at: Mapped[datetime] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(500))
    report_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    create_time: Mapped[datetime] = mapped_column(DateTime)
    update_time: Mapped[datetime] = mapped_column(DateTime)


class UploadSession(Base):
    __tablename__ = "t_upload_session"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    original_file_name: Mapped[str] = mapped_column(String(200))
    file_size: Mapped[int] = mapped_column(BigInteger)
    content_sha256: Mapped[str] = mapped_column(String(64))
    chunk_size: Mapped[int] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    file_id: Mapped[int | None] = mapped_column(BigInteger)
    create_time: Mapped[datetime] = mapped_column(DateTime)
    update_time: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("t_user.id"))


class UploadChunk(Base):
    __tablename__ = "t_upload_chunk"

    upload_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("t_upload_session.id"), primary_key=True)
    chunk_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    chunk_size: Mapped[int] = mapped_column(Integer)
    chunk_sha256: Mapped[str] = mapped_column(String(64))


class ChatConversation(Base):
    __tablename__ = "t_chat_conversation"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("t_user.id", ondelete="CASCADE"))
    file_id: Mapped[int] = mapped_column(ForeignKey("t_ai_customer_service_md_storage.id", ondelete="CASCADE"))
    active_request_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class ChatTurn(Base):
    __tablename__ = "t_chat_turn"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("t_chat_conversation.id", ondelete="CASCADE"))
    request_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), unique=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    evidence_snapshot: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class AnswerFeedback(Base):
    __tablename__ = "t_answer_feedback"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("t_user.id", ondelete="CASCADE"))
    request_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True))
    file_id: Mapped[int | None] = mapped_column(ForeignKey("t_ai_customer_service_md_storage.id", ondelete="SET NULL"))
    category: Mapped[str] = mapped_column(String(32))
    user_note: Mapped[str] = mapped_column(Text)
    snapshot: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    diagnosis: Mapped[str] = mapped_column(String(24), default="unknown")
    resolution: Mapped[str | None] = mapped_column(String(24))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("t_release_eval_case.id", ondelete="SET NULL"))
    regression_file_id: Mapped[int | None] = mapped_column(ForeignKey("t_ai_customer_service_md_storage.id", ondelete="SET NULL"))
    report_id: Mapped[UUID | None] = mapped_column(ForeignKey("t_release_eval_run.id", ondelete="SET NULL"))
    closure_evidence: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class FeedbackEvent(Base):
    __tablename__ = "t_feedback_event"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    feedback_id: Mapped[int] = mapped_column(ForeignKey("t_answer_feedback.id", ondelete="CASCADE"))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("t_user.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


@lru_cache(maxsize=1)
def session_factory():
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL 未配置")
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def db_session():
    session: Session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
