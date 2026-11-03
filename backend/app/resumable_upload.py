"""Durable, checksummed Markdown uploads. DB rows and parts live on one persistent volume."""

from datetime import datetime, timedelta
import hashlib
import os
from pathlib import Path
import re
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text

from app.config import settings
from app.db import MarkdownFile, UploadChunk, UploadSession, db_session
from app.documents import DocumentReadError, document_format, require_format_available, validate_document
from app.access import management_scope


CHUNK_SIZE = 4 * 1024 * 1024
MAX_FILE_SIZE = 256 * 1024 * 1024
MAX_ACTIVE_SESSIONS = 100
MAX_RESERVED_BYTES = 2 * 1024 * 1024 * 1024
SESSION_LIFETIME = timedelta(days=7)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class UploadInitRequest(BaseModel):
    fileName: str = Field(min_length=1, max_length=200)
    fileSize: int = Field(gt=0, le=MAX_FILE_SIZE)
    sha256: str

    @field_validator("fileName")
    @classmethod
    def validate_name(cls, name: str) -> str:
        name = name.strip()
        if Path(name).name != name or "\\" in name or "/" in name:
            raise ValueError("文件名不允许包含路径")
        document_format(name)
        return name

    @field_validator("sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        value = value.lower()
        if not SHA256.fullmatch(value):
            raise ValueError("sha256 格式错误")
        return value


def _parts_dir(upload_id: UUID) -> Path:
    return settings.md_storage_path / ".uploads" / str(upload_id)


def _part_path(upload_id: UUID, index: int) -> Path:
    return _parts_dir(upload_id) / f"{index:06d}.part"


def _file_matches(file: MarkdownFile) -> bool:
    path = Path(file.file_path)
    try:
        if not path.is_file() or path.stat().st_size != file.file_size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while data := source.read(1024 * 1024):
                digest.update(data)
        return digest.hexdigest() == file.content_sha256
    except OSError:
        return False


def _require_session(db, upload_id: UUID, user, lock: bool = False) -> UploadSession:
    query = select(UploadSession).where(UploadSession.id == upload_id)
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None or (user.role != "admin" and row.owner_id != user.id):
        raise HTTPException(404, "上传任务不存在或无权限")
    if row.status == "expired" or (row.status == "uploading" and row.expires_at < datetime.now()):
        raise HTTPException(410, "上传任务已过期，请重新选择文件")
    return row


def _valid_chunks(db, row: UploadSession) -> list[int]:
    chunks = db.scalars(select(UploadChunk).where(UploadChunk.upload_id == row.id)).all()
    return sorted(chunk.chunk_index for chunk in chunks
                  if _part_path(row.id, chunk.chunk_index).is_file()
                  and _part_path(row.id, chunk.chunk_index).stat().st_size == chunk.chunk_size)


def init_upload(payload: UploadInitRequest, user) -> dict:
    require_format_available(payload.fileName)
    now = datetime.now()
    with db_session() as db:
        # Existing bytes can be reused even while their indexing is still running.
        existing_files = db.scalars(select(MarkdownFile).where(
            management_scope(user),
            MarkdownFile.content_sha256 == payload.sha256,
            MarkdownFile.file_size == payload.fileSize,
            MarkdownFile.status.in_((0, 1, 2))).order_by(MarkdownFile.id)).all()
        for existing in existing_files:
            if document_format(existing.original_file_name) == document_format(payload.fileName) and _file_matches(existing):
                return {"instant": True, "fileId": existing.id, "status": existing.status}
        row = db.scalar(select(UploadSession).where(
            UploadSession.content_sha256 == payload.sha256,
            UploadSession.file_size == payload.fileSize,
            UploadSession.original_file_name == payload.fileName,
            UploadSession.status == "uploading",
            UploadSession.owner_id == user.id,
            UploadSession.expires_at > now,
        ).order_by(UploadSession.create_time.desc()).limit(1))
        if row is None:
            active_count, reserved_bytes = db.execute(select(
                func.count(UploadSession.id), func.coalesce(func.sum(UploadSession.file_size), 0)
            ).where(UploadSession.status == "uploading", UploadSession.expires_at > now)).one()
            if active_count >= MAX_ACTIVE_SESSIONS or reserved_bytes + payload.fileSize > MAX_RESERVED_BYTES:
                raise HTTPException(429, "待完成上传任务过多，请稍后重试或清理过期任务")
            row = UploadSession(id=uuid4(), original_file_name=payload.fileName,
                                file_size=payload.fileSize, content_sha256=payload.sha256,
                                chunk_size=CHUNK_SIZE,
                                chunk_count=(payload.fileSize + CHUNK_SIZE - 1) // CHUNK_SIZE,
                                status="uploading", file_id=None, create_time=now,
                                update_time=now, expires_at=now + SESSION_LIFETIME, owner_id=user.id)
            db.add(row)
            db.flush()
        uploaded = _valid_chunks(db, row)
        return {"instant": False, "uploadId": str(row.id), "chunkSize": row.chunk_size,
                "totalChunks": row.chunk_count, "uploadedChunks": uploaded,
                "expiresAt": row.expires_at.isoformat(timespec="seconds")}


def upload_status(upload_id: UUID, user) -> dict:
    with db_session() as db:
        row = _require_session(db, upload_id, user)
        return {"uploadId": str(row.id), "status": row.status, "fileId": row.file_id,
                "chunkSize": row.chunk_size, "totalChunks": row.chunk_count,
                "uploadedChunks": _valid_chunks(db, row) if row.status == "uploading" else []}


async def put_chunk(upload_id: UUID, index: int, expected_hash: str, request: Request, user) -> dict:
    expected_hash = expected_hash.lower()
    if not SHA256.fullmatch(expected_hash):
        raise HTTPException(422, "分片 SHA-256 格式错误")
    with db_session() as db:
        row = _require_session(db, upload_id, user)
        if row.status != "uploading" or index < 0 or index >= row.chunk_count:
            raise HTTPException(409, "上传任务已结束或分片序号非法")
        expected_size = min(row.chunk_size, row.file_size - index * row.chunk_size)

    parts = _parts_dir(upload_id)
    parts.mkdir(parents=True, exist_ok=True)
    temporary = parts / f"{index:06d}.{uuid4().hex}.tmp"
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("wb") as target:
            async for data in request.stream():
                size += len(data)
                if size > expected_size:
                    raise HTTPException(413, "分片大小超过预期")
                target.write(data)
                digest.update(data)
        if size != expected_size or digest.hexdigest() != expected_hash:
            raise HTTPException(422, "分片长度或校验和不匹配")
        with db_session() as db:
            row = _require_session(db, upload_id, user, lock=True)
            if row.status != "uploading":
                raise HTTPException(409, "上传任务已结束")
            saved = db.get(UploadChunk, (upload_id, index))
            if saved and saved.chunk_sha256 != expected_hash:
                raise HTTPException(409, "该序号已上传不同内容")
            # Atomic replacement; status only advertises rows whose parts still exist.
            os.replace(temporary, _part_path(upload_id, index))
            if saved is None:
                db.add(UploadChunk(upload_id=upload_id, chunk_index=index,
                                   chunk_size=size, chunk_sha256=expected_hash))
            row.update_time = datetime.now()
        return {"index": index, "sha256": expected_hash}
    finally:
        temporary.unlink(missing_ok=True)


def complete_upload(upload_id: UUID, user) -> dict:
    storage = settings.md_storage_path
    storage.mkdir(parents=True, exist_ok=True)
    temporary = storage / f".{upload_id}.merging"
    created_target = None
    try:
        with db_session() as db:
            row = _require_session(db, upload_id, user, lock=True)
            if row.status == "completed":
                return {"fileId": row.file_id, "status": "completed", "instant": False,
                        "alreadyCompleted": True}
            # Serialize completion of distinct sessions carrying the same content.
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:digest))"),
                       {"digest": row.content_sha256})
            if len(_valid_chunks(db, row)) != row.chunk_count:
                raise HTTPException(409, "分片尚未全部上传")
            chunks = {chunk.chunk_index: chunk for chunk in db.scalars(
                select(UploadChunk).where(UploadChunk.upload_id == upload_id)).all()}
            digest = hashlib.sha256()
            total = 0
            with temporary.open("wb") as target:
                for index in range(row.chunk_count):
                    part = _part_path(upload_id, index)
                    part_digest = hashlib.sha256()
                    with part.open("rb") as source:
                        while data := source.read(1024 * 1024):
                            total += len(data)
                            digest.update(data)
                            part_digest.update(data)
                            target.write(data)
                    if part_digest.hexdigest() != chunks[index].chunk_sha256:
                        raise HTTPException(422, f"分片 {index} 校验失败，请重新上传")
            if total != row.file_size or digest.hexdigest() != row.content_sha256:
                raise HTTPException(422, "合并后的文件校验失败，请重新上传")
            validate_document(temporary, row.original_file_name)
            existing_files = db.scalars(select(MarkdownFile).where(
                management_scope(user),
                MarkdownFile.content_sha256 == row.content_sha256,
                MarkdownFile.file_size == row.file_size,
                MarkdownFile.status.in_((0, 1, 2))).order_by(MarkdownFile.id)).all()
            existing = next((file for file in existing_files
                             if document_format(file.original_file_name) == document_format(row.original_file_name)
                             and _file_matches(file)), None)
            if existing:
                row.file_id = existing.id
                instant = True
            else:
                # UUID name avoids collisions; no untrusted filename is used as a path.
                name = f"{uuid4()}{Path(row.original_file_name).suffix.lower()}"
                target = storage / name
                os.replace(temporary, target)
                created_target = target
                now = datetime.now()
                file = MarkdownFile(original_file_name=row.original_file_name,
                                    new_file_name=name, file_path=str(target),
                                    file_size=row.file_size, content_sha256=row.content_sha256,
                                    status=0, remark=None, create_time=now, update_time=now,
                                    owner_id=row.owner_id)
                db.add(file)
                db.flush()
                row.file_id = file.id
                from app.task_queue import enqueue
                enqueue(db, file.id)
                instant = False
            row.status = "completed"
            row.update_time = datetime.now()
            file_id = row.file_id
        # Parts are recoverable until DB commit; only remove after successful commit.
        try:
            for part in _parts_dir(upload_id).glob("*"):
                part.unlink(missing_ok=True)
            _parts_dir(upload_id).rmdir()
        except OSError:
            # The committed file is usable; maintenance can remove stale parts later.
            pass
        return {"fileId": file_id, "status": "completed", "instant": instant,
                "alreadyCompleted": False}
    except DocumentReadError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        if created_target is not None:
            created_target.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
