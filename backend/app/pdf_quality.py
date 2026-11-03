"""Page-level PDF extraction audit; this is triage, not OCR or clinical validation."""

from datetime import datetime
import hashlib
from pathlib import Path
import subprocess
import tempfile

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from pypdf import PdfReader
from sqlalchemy import delete, select, text

from app.access import require_file_manager
from app.audit import audit
from app.auth import editor_required
from app.config import PROJECT_ROOT, settings
from app.contracts import ok
from app.db import MarkdownFile, PdfPageAudit, User, db_session
from app.documents import read_documents
from app.storage_guard import storage_write_guard


router = APIRouter(prefix="/customer-service/md/pdf", tags=["pdf-quality"])


class FileRequest(BaseModel):
    id: int = Field(gt=0)


class PageRequest(FileRequest):
    pageNumber: int = Field(gt=0)


class ReviewRequest(PageRequest):
    decision: str = Field(pattern="^(approved|excluded)$")
    note: str = Field(min_length=5, max_length=500)


def _file(db, file_id: int, user: User) -> MarkdownFile:
    row = db.get(MarkdownFile, file_id)
    require_file_manager(row, user)
    if Path(row.original_file_name).suffix.lower() != ".pdf":
        raise HTTPException(422, "仅支持 PDF 页级验收")
    return row


def _source_path(row: MarkdownFile) -> Path:
    path = Path(row.file_path).resolve()
    roots = (settings.md_storage_path, PROJECT_ROOT / "data" / "public_manuals")
    if not any(path.is_relative_to(root.resolve()) for root in roots) or not path.is_file():
        raise HTTPException(409, "原 PDF 不在受控存储目录或已丢失")
    return path


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _chunk_rows(db, file_id: int):
    return db.execute(text("""
        SELECT (metadata->>'pdfPage')::integer AS page_number, content
        FROM t_vector_store
        WHERE metadata->>'mdStorageId'=:id AND (metadata->>'pdfPage') ~ '^[0-9]+$'
        ORDER BY (metadata->>'pdfPage')::integer, metadata->>'chunkIndex'
    """), {"id": str(file_id)}).all()


def _indexed_by_page(db, file_id: int) -> dict[int, list[str]]:
    chunks = {}
    for page_number, content in _chunk_rows(db, file_id):
        chunks.setdefault(page_number, []).append(content)
    return chunks


def create_page_audit(db, row: MarkdownFile) -> dict:
    """Backfill an indexed draft without touching vectors, embeddings or source PDF."""
    if row.status != 2 or row.is_published or row.review_status not in (None, "draft", "rejected", "pending_review"):
        raise HTTPException(409, "只能对处理完成且未发布的草稿生成页级验收")
    path = _source_path(row)
    source_sha = _sha(path)
    if row.content_sha256 and source_sha != row.content_sha256:
        raise HTTPException(409, "原 PDF 哈希与入库记录不符，不能沿用现有索引")
    if db.scalar(select(PdfPageAudit.file_id).where(PdfPageAudit.file_id == row.id).limit(1)):
        raise HTTPException(409, "已有页级验收，重新索引新版本后才能重建，避免覆盖人工结论")
    pages = []
    documents = read_documents(path, row.original_file_name, page_audits=pages)
    indexed = _indexed_by_page(db, row.id)
    generated = {}
    for chunk in documents:
        page = chunk.metadata.get("pdfPage")
        if page:
            generated[page] = generated.get(page, 0) + 1
    for item in pages:
        page = item["pageNumber"]
        flags = list(item["flags"])
        if item["extractedText"] and (not indexed.get(page) or len(indexed[page]) != generated.get(page, 0)):
            flags.append("indexed_chunks_mismatch")
        if not item["extractedText"] and indexed.get(page):
            flags.append("unexpected_indexed_chunk")
        status = "needs_review" if flags and item["extractedText"] else item["status"]
        db.add(PdfPageAudit(file_id=row.id, page_number=page, page_label=item["pageLabel"],
                            extracted_text=item["extractedText"], flags=flags, status=status,
                            source_sha256=source_sha))
    row.pdf_qa_revision = (row.pdf_qa_revision or 0) + 1
    db.flush()
    return audit_summary(db, row)


def audit_summary(db, row: MarkdownFile) -> dict:
    pages = db.scalars(select(PdfPageAudit).where(PdfPageAudit.file_id == row.id)
                       .order_by(PdfPageAudit.page_number)).all()
    indexed = _indexed_by_page(db, row.id)
    expected = len(PdfReader(_source_path(row)).pages)
    source_sha = _sha(_source_path(row))
    ready = (len(pages) == expected and bool(pages)
             and all(p.source_sha256 == source_sha and p.status != "needs_review" for p in pages)
             and any(p.status in ("auto_ok", "approved") and indexed.get(p.page_number) for p in pages)
             and all(p.status not in ("auto_ok", "approved") or indexed.get(p.page_number) for p in pages))
    return {
        "fileId": row.id, "fileName": row.original_file_name, "sourceSha256": row.content_sha256,
        "pageCount": expected, "auditedPages": len(pages),
        "needsReview": sum(p.status == "needs_review" for p in pages),
        "excluded": sum(p.status == "excluded" for p in pages),
        "ready": ready,
        "pages": [{"pageNumber": p.page_number, "pageLabel": p.page_label, "status": p.status,
                   "flags": p.flags, "extractedChars": len(p.extracted_text),
                   "indexedChunks": len(indexed.get(p.page_number, [])),
                   "reviewerId": p.reviewer_id, "note": p.note} for p in pages],
    }


def require_pdf_audit_ready(db, row: MarkdownFile):
    path = _source_path(row)
    source_sha = _sha(path)
    if row.content_sha256 and source_sha != row.content_sha256:
        raise HTTPException(409, "PDF 原文件已变更，禁止发布")
    pages = db.scalars(select(PdfPageAudit).where(PdfPageAudit.file_id == row.id)).all()
    if len(pages) != len(PdfReader(path).pages) or not pages or any(
        p.source_sha256 != source_sha or p.status == "needs_review" for p in pages
    ):
        raise HTTPException(409, "PDF 页级验收未完成或已过期：请复核风险页")
    indexed = _indexed_by_page(db, row.id)
    if not any(p.status in ("auto_ok", "approved") and indexed.get(p.page_number) for p in pages):
        raise HTTPException(409, "PDF 没有可用的已验收索引片段")
    if any(p.status in ("auto_ok", "approved") and not indexed.get(p.page_number) for p in pages):
        raise HTTPException(409, "PDF 存在已验收但未入向量库的页面")


@router.post("/audit", dependencies=[Depends(storage_write_guard)])
def generate_audit(payload: FileRequest, user: User = Depends(editor_required)):
    with db_session() as db:
        row = db.scalar(select(MarkdownFile).where(MarkdownFile.id == payload.id).with_for_update())
        require_file_manager(row, user)
        result = create_page_audit(db, _file(db, payload.id, user))
        audit(db, "pdf_page_audit_create", row.id, f"pages={result['pageCount']}", actor=user)
        return ok(result)


@router.post("/pages")
def list_pages(payload: FileRequest, user: User = Depends(editor_required)):
    with db_session() as db:
        return ok(audit_summary(db, _file(db, payload.id, user)))


@router.post("/page")
def page_detail(payload: PageRequest, user: User = Depends(editor_required)):
    with db_session() as db:
        _file(db, payload.id, user)
        row = db.get(PdfPageAudit, (payload.id, payload.pageNumber))
        if row is None:
            raise HTTPException(404, "页级验收记录不存在")
        chunks = _indexed_by_page(db, payload.id).get(payload.pageNumber, [])
        return ok({"pageNumber": row.page_number, "pageLabel": row.page_label,
                   "extractedText": row.extracted_text, "indexedChunks": chunks,
                   "status": row.status, "flags": row.flags, "note": row.note,
                   "reviewerId": row.reviewer_id})


@router.post("/review", dependencies=[Depends(storage_write_guard)])
def review_page(payload: ReviewRequest, user: User = Depends(editor_required)):
    with db_session() as db:
        file = db.scalar(select(MarkdownFile).where(MarkdownFile.id == payload.id).with_for_update())
        require_file_manager(file, user)
        if file.status != 2 or file.is_published or file.review_status not in (None, "draft", "rejected", "pending_review"):
            raise HTTPException(409, "仅允许复核未发布的已处理 PDF")
        _file(db, payload.id, user)
        row = db.get(PdfPageAudit, (payload.id, payload.pageNumber))
        if row is None:
            raise HTTPException(404, "页级验收记录不存在")
        if row.source_sha256 != _sha(_source_path(file)):
            raise HTTPException(409, "原 PDF 已变化，请勿沿用旧复核结论")
        if payload.decision == "approved" and (not row.extracted_text or
                not _indexed_by_page(db, file.id).get(row.page_number) or
                "indexed_chunks_mismatch" in row.flags):
            raise HTTPException(409, "该页没有可信的索引片段；请排除或上传修正后的新版本")
        row.status = payload.decision
        row.note = payload.note.strip()
        row.reviewer_id = user.id
        row.reviewed_at = datetime.now()
        file.pdf_qa_revision = (file.pdf_qa_revision or 0) + 1
        audit(db, "pdf_page_review", file.id, f"page={row.page_number}; decision={row.status}; {row.note}", actor=user)
        return ok({"status": row.status, "pageNumber": row.page_number})


@router.get("/preview/{file_id}/{page_number}")
def preview_page(file_id: int, page_number: int, user: User = Depends(editor_required)):
    with db_session() as db:
        row = _file(db, file_id, user)
        path = _source_path(row)
        if page_number < 1 or page_number > len(PdfReader(path).pages):
            raise HTTPException(404, "PDF 页码不存在")
    try:
        with tempfile.TemporaryDirectory(prefix="pdf-page-preview-") as folder:
            prefix = Path(folder) / "page"
            subprocess.run(["pdftoppm", "-f", str(page_number), "-l", str(page_number),
                            "-singlefile", "-scale-to", "1100", "-png", str(path), str(prefix)],
                           capture_output=True, check=True, timeout=30)
            png = prefix.with_suffix(".png").read_bytes()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(503, "PDF 页面预览生成失败") from exc
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, no-store"})
