"""Reviewed, immutable document versions layered over the existing file/worker pipeline."""

from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import delete, func, select, text

from app.access import require_file_manager
from app.audit import audit
from app.auth import admin_required, editor_required
from app.db import FileGrant, MarkdownFile, User, db_session
from app.storage_guard import storage_write_guard
from app.contracts import ok
from app.release_quality import require_passing_release_eval
from app.config import settings


router = APIRouter(prefix="/customer-service/md/versions", tags=["document-versions"])


class RegisterVersionRequest(BaseModel):
    id: int = Field(gt=0)
    baseId: int | None = Field(default=None, gt=0)
    revisionLabel: str = Field(min_length=1, max_length=100)
    sourceUrl: HttpUrl | None = None


class VersionIdRequest(BaseModel):
    id: int = Field(gt=0)


class ReviewVersionRequest(VersionIdRequest):
    approve: bool
    note: str = Field(default="", max_length=500)
    localDemoSelfApproval: bool = False


def _self_approval_exception(payload, row, user):
    if not payload.approve or row.owner_id != user.id:
        return False
    if not settings.local_demo_allow_self_approval:
        raise HTTPException(409, "审批人与资料上传者必须是不同账号；本地演示例外审批未开启")
    if user.role != "admin":
        raise HTTPException(403, "单管理员例外审批仅允许系统管理员")
    if not payload.localDemoSelfApproval or len(payload.note.strip()) < 5:
        raise HTTPException(422, "本地演示自审批需明确确认例外，并填写至少5字的审批原因")
    return True


@router.get("/policy")
def approval_policy(user: User = Depends(editor_required)):
    return ok({"allowSelfApproval": settings.local_demo_allow_self_approval and user.role == "admin",
               "scope": "local-demo-only" if settings.local_demo_allow_self_approval else "independent-reviewer"})


class RollbackVersionRequest(VersionIdRequest):
    reason: str = Field(min_length=1, max_length=500)


class HistoryRequest(BaseModel):
    documentKey: UUID


def _lock_document(db, document_key: UUID):
    # Every mutation of one logical document uses the same transaction-level lock.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key)::bigint)"), {"key": str(document_key)})


def _version(db, file_id: int, user: User):
    row = db.get(MarkdownFile, file_id)
    require_file_manager(row, user)
    if row.document_key is None:
        raise HTTPException(409, "请先将资料登记为版本")
    return row


def _versions(db, document_key: UUID):
    return db.scalars(select(MarkdownFile).where(MarkdownFile.document_key == document_key)
                      .order_by(MarkdownFile.version_number).with_for_update()).all()


def _copy_grants(db, source_id: int, target_id: int):
    # Access policy follows the live logical document on switch/rollback.
    user_ids = db.scalars(select(FileGrant.user_id).where(FileGrant.file_id == source_id)).all()
    db.execute(delete(FileGrant).where(FileGrant.file_id == target_id))
    db.flush()
    for user_id in user_ids:
        db.add(FileGrant(file_id=target_id, user_id=user_id))


@router.post("/register", dependencies=[Depends(storage_write_guard)])
def register_version(payload: RegisterVersionRequest, user: User = Depends(editor_required)):
    """Attach a file uploaded by either small or resumable upload as v1/vN."""
    with db_session() as db:
        row = db.scalar(select(MarkdownFile).where(MarkdownFile.id == payload.id).with_for_update())
        require_file_manager(row, user)
        if not payload.revisionLabel.strip():
            raise HTTPException(422, "修订标识不能为空")
        if row.owner_id is None and not row.is_published:
            raise HTTPException(409, "未指定负责人的旧资料不能创建待审核版本")
        if row.document_key is not None:
            raise HTTPException(409, "文件已登记，不能重复归入其他版本")
        if payload.baseId is None:
            document_key, version_number = uuid4(), 1
            status = "approved" if row.is_published and row.status == 2 else "draft"
            if row.is_published and row.status != 2:
                raise HTTPException(409, "处理中资料不能登记为历史已发布版本")
        else:
            base = _version(db, payload.baseId, user)
            if base.id == row.id or base.owner_id != row.owner_id:
                raise HTTPException(409, "新版本必须由同一资料负责人上传的不同文件创建")
            document_key = base.document_key
            _lock_document(db, document_key)
            # Recheck after waiting on the lock; concurrent registrations cannot reuse a number.
            db.refresh(row)
            if row.document_key is not None:
                raise HTTPException(409, "文件已登记")
            version_number = (db.scalar(select(func.max(MarkdownFile.version_number)).where(
                MarkdownFile.document_key == document_key)) or 0) + 1
            status = "draft"
            if row.is_published:
                raise HTTPException(409, "已发布文件不能作为待审核的新版本")
        row.document_key = document_key
        row.version_number = version_number
        row.review_status = status
        row.revision_label = payload.revisionLabel.strip()
        row.source_url = str(payload.sourceUrl) if payload.sourceUrl else None
        # Indexing and registration run independently. If the Worker already
        # committed vectors, publish the checked source on those rows too;
        # otherwise index_markdown reads source_url at its commit fence.
        if row.source_url:
            db.execute(text("""UPDATE t_vector_store
                               SET metadata=(metadata::jsonb || jsonb_build_object('sourceUrl', CAST(:url AS text)))::json
                               WHERE metadata->>'mdStorageId'=:id"""),
                       {"id": str(row.id), "url": row.source_url})
        row.update_time = datetime.now()
        audit(db, "version_register", row.id, f"{document_key} v{version_number}", actor=user)
        return ok({"documentKey": str(document_key), "version": version_number,
                   "reviewStatus": status, "id": row.id})


@router.post("/submit", dependencies=[Depends(storage_write_guard)])
def submit_version(payload: VersionIdRequest, user: User = Depends(editor_required)):
    with db_session() as db:
        submit_in_transaction(db, payload.id, user)
    return ok()


def submit_in_transaction(db, file_id, user):
    """Shared by explicit HTTP submit and version-bound Agent human confirmation."""
    row = _version(db, file_id, user)
    _lock_document(db, row.document_key)
    db.refresh(row, with_for_update=True)
    if row.review_status not in ("draft", "rejected") or row.status != 2 or row.is_published:
        raise HTTPException(409, "仅处理完成的草稿或驳回版本可以送审")
    row.review_status = "pending_review"
    row.submitted_at = datetime.now()
    row.reviewer_id = None
    row.reviewed_at = None
    row.review_note = None
    row.update_time = datetime.now()
    audit(db, "version_submit", row.id, f"v{row.version_number}", actor=user)


@router.post("/review", dependencies=[Depends(storage_write_guard)])
def review_version(payload: ReviewVersionRequest, user: User = Depends(admin_required)):
    with db_session() as db:
        row = _version(db, payload.id, user)
        _lock_document(db, row.document_key)
        versions = _versions(db, row.document_key)
        row = next(item for item in versions if item.id == payload.id)
        if row.review_status != "pending_review" or row.status != 2:
            raise HTTPException(409, "仅处理完成、待审核的版本可以审核")
        self_approval = _self_approval_exception(payload, row, user)
        previous = next((item for item in versions if item.is_published), None)
        if payload.approve:
            if row.original_file_name.lower().endswith(".pdf"):
                from app.pdf_quality import require_pdf_audit_ready
                require_pdf_audit_ready(db, row)
            if previous and row.version_number <= previous.version_number:
                raise HTTPException(409, "只能批准比当前发布版本更新的版本；恢复旧版请使用回滚")
            evaluation_id = require_passing_release_eval(db, row, previous)
            if previous:
                previous.is_published = False
                previous.review_status = "superseded"
                previous.update_time = datetime.now()
                _copy_grants(db, previous.id, row.id)
            db.flush()  # Release unique published-document index before activating new row.
            row.is_published = True
            row.review_status = "approved"
        else:
            if not payload.note.strip():
                raise HTTPException(422, "驳回时必须填写原因")
            row.review_status = "rejected"
        row.reviewed_at = datetime.now()
        row.reviewer_id = user.id
        row.review_note = (("[本地演示单管理员例外审批；非独立审核] " + payload.note.strip())[:500]
                           if self_approval else payload.note.strip() or None)
        row.update_time = datetime.now()
        audit(db, "version_approve" if payload.approve else "version_reject", row.id,
              f"v{row.version_number}; previous={previous.id if previous else 'none'}; "
              f"eval={evaluation_id if payload.approve else 'none'}; {row.review_note or ''}", actor=user)
        if self_approval:
            audit(db, "version_self_approve_local_demo", row.id,
                  f"v{row.version_number}; owner=reviewer={user.id}; eval={evaluation_id}; {row.review_note}", actor=user)
    return ok()


@router.post("/rollback", dependencies=[Depends(storage_write_guard)])
def rollback_version(payload: RollbackVersionRequest, user: User = Depends(admin_required)):
    if not payload.reason.strip():
        raise HTTPException(422, "请填写回滚原因")
    with db_session() as db:
        target = _version(db, payload.id, user)
        _lock_document(db, target.document_key)
        versions = _versions(db, target.document_key)
        target = next(item for item in versions if item.id == payload.id)
        current = next((item for item in versions if item.is_published), None)
        if target.review_status != "superseded" or target.status != 2 or current is None:
            raise HTTPException(409, "只能回滚到已完成、曾经审核发布的旧版本")
        if target.owner_id != current.owner_id:
            raise HTTPException(409, "版本负责人不一致，拒绝回滚")
        current.is_published = False
        current.review_status = "superseded"
        current.update_time = datetime.now()
        _copy_grants(db, current.id, target.id)
        db.flush()
        target.is_published = True
        target.review_status = "approved"
        target.reviewer_id = user.id
        target.reviewed_at = datetime.now()
        target.review_note = payload.reason.strip()
        target.update_time = datetime.now()
        audit(db, "version_rollback", target.id,
              f"from={current.id} to={target.id}; {payload.reason.strip()}", actor=user)
    return ok()


@router.post("/history")
def version_history(payload: HistoryRequest, user: User = Depends(editor_required)):
    with db_session() as db:
        rows = db.scalars(select(MarkdownFile).where(MarkdownFile.document_key == payload.documentKey)
                          .order_by(MarkdownFile.version_number.desc())).all()
        if not rows:
            raise HTTPException(404, "资料版本不存在")
        require_file_manager(rows[0], user)
        return ok([{
            "id": row.id, "version": row.version_number, "fileName": row.original_file_name,
            "reviewStatus": row.review_status, "isPublished": row.is_published,
            "revisionLabel": row.revision_label, "sourceUrl": row.source_url,
            "sha256": row.content_sha256, "status": row.status,
            "submittedAt": row.submitted_at.isoformat() if row.submitted_at else None,
            "reviewedAt": row.reviewed_at.isoformat() if row.reviewed_at else None,
            "reviewerId": row.reviewer_id, "reviewNote": row.review_note,
            "embeddingModel": row.embedding_model, "indexConfig": row.index_config,
        } for row in rows])
