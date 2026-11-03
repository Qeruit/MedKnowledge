from fastapi import HTTPException
from sqlalchemy import and_, exists, or_, select

from app.db import FileGrant, MarkdownFile


def management_scope(user):
    return True if user.role == "admin" else MarkdownFile.owner_id == user.id


def require_file_manager(row, user):
    if row is None or (user.role != "admin" and row.owner_id != user.id):
        raise HTTPException(404, "资料不存在或无管理权限")


def readable_scope(user):
    grant = exists(select(FileGrant.file_id).where(FileGrant.file_id == MarkdownFile.id,
                                                  FileGrant.user_id == user.id))
    return and_(MarkdownFile.is_published.is_(True),
                or_(MarkdownFile.document_key.is_(None), MarkdownFile.review_status == "approved")) & (
        True if user.role == "admin" else or_(MarkdownFile.owner_id == user.id, grant))
