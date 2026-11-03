"""Version-scoped, reproducible RAG release gate. Evaluation never publishes draft evidence."""

from datetime import datetime
import hashlib
import json
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from app.access import require_file_manager
from app.answer_prompt import NO_EVIDENCE_REPLY, answer_messages, evidence_reply
from app.audit import audit
from app.auth import admin_required, editor_required
from app.config import settings
from app.contracts import ok
from app.db import MarkdownFile, ReleaseEvalCase, ReleaseEvalJob, ReleaseEvalRun, User, db_session
from app.knowledge import find_context
from app.llm import chat_chunks, require_llm
from app.quality_queue import enqueue_quality


router = APIRouter(prefix="/customer-service/md/quality", tags=["release-quality"])
MAX_CASES = 20  # At most 40 model calls, now handled by a dedicated Worker.
PROMPT_REVISION = hashlib.sha256(Path(__file__).with_name("answer_prompt.py").read_bytes()).hexdigest()
RETRIEVAL_REVISION = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                      for name in ("knowledge.py", "pdf_rerank.py", "splitter.py")}


class CaseRequest(BaseModel):
    documentKey: UUID
    question: str = Field(min_length=2, max_length=500)
    kind: str = Field(pattern="^(fact|no_evidence)$")
    evidenceTerms: list[str] = Field(default_factory=list, max_length=8)
    answerTerms: list[str] = Field(default_factory=list, max_length=8)
    critical: bool = True

    @model_validator(mode="after")
    def validate_labels(self):
        self.question = self.question.strip()
        self.evidenceTerms = [value.strip() for value in self.evidenceTerms]
        self.answerTerms = [value.strip() for value in self.answerTerms]
        if any(not value or len(value) > 120 for value in self.evidenceTerms + self.answerTerms):
            raise ValueError("标注词必须为 1–120 字符")
        if self.kind == "fact" and (not self.evidenceTerms or not self.answerTerms):
            raise ValueError("事实题必须标注应命中的证据和回答事实")
        if self.kind == "no_evidence" and (self.evidenceTerms or self.answerTerms):
            raise ValueError("拒答题不应标注证据或事实")
        return self


class DocumentRequest(BaseModel):
    documentKey: UUID


class CaseIdRequest(BaseModel):
    id: int = Field(gt=0)


class CandidateRequest(BaseModel):
    id: int = Field(gt=0)


def _document(db, key: UUID, user: User):
    row = db.scalar(select(MarkdownFile).where(MarkdownFile.document_key == key).limit(1))
    require_file_manager(row, user)
    return row


def _cases(db, key: UUID):
    return db.scalars(select(ReleaseEvalCase).where(ReleaseEvalCase.document_key == key)
                      .order_by(ReleaseEvalCase.id)).all()


def _case_data(case: ReleaseEvalCase):
    return {"id": case.id, "question": case.question, "kind": case.kind,
            "evidenceTerms": case.evidence_terms, "answerTerms": case.answer_terms,
            "critical": case.critical}


def _fingerprint(candidate: MarkdownFile, baseline: MarkdownFile | None, cases: list[ReleaseEvalCase]):
    payload = {
        "candidate": [candidate.id, candidate.content_sha256, candidate.status,
                      candidate.review_status, candidate.submitted_at.isoformat() if candidate.submitted_at else None,
                      candidate.embedding_model, candidate.index_config, candidate.pdf_qa_revision],
        "baseline": [baseline.id, baseline.content_sha256, baseline.embedding_model,
                     baseline.index_config] if baseline else None,
        "cases": [_case_data(case) for case in cases],
        "model": settings.customer_model, "embedding": settings.embedding_model,
        "prompt": PROMPT_REVISION,
        "retrievalCode": RETRIEVAL_REVISION,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()


def _snapshot(db, file_id: int, user: User):
    candidate = db.get(MarkdownFile, file_id)
    require_file_manager(candidate, user)
    if candidate.document_key is None:
        raise HTTPException(409, "请先登记资料版本")
    baseline = db.scalar(select(MarkdownFile).where(MarkdownFile.document_key == candidate.document_key,
                                                    MarkdownFile.is_published.is_(True)))
    cases = _cases(db, candidate.document_key)
    return candidate, baseline, cases


@router.post("/cases")
def list_cases(payload: DocumentRequest, user: User = Depends(editor_required)):
    with db_session() as db:
        _document(db, payload.documentKey, user)
        return ok([_case_data(case) for case in _cases(db, payload.documentKey)])


@router.post("/cases/add")
def add_case(payload: CaseRequest, user: User = Depends(admin_required)):
    with db_session() as db:
        _document(db, payload.documentKey, user)
        if len(_cases(db, payload.documentKey)) >= MAX_CASES:
            raise HTTPException(409, f"每份资料最多 {MAX_CASES} 条发布评测题")
        case = ReleaseEvalCase(document_key=payload.documentKey, question=payload.question,
                               kind=payload.kind, evidence_terms=payload.evidenceTerms,
                               answer_terms=payload.answerTerms, critical=payload.critical,
                               create_time=datetime.now())
        db.add(case)
        db.flush()
        audit(db, "quality_case_add", None, f"{payload.documentKey}; case={case.id}", actor=user)
        return ok(_case_data(case))


@router.post("/cases/delete")
def delete_case(payload: CaseIdRequest, user: User = Depends(admin_required)):
    with db_session() as db:
        case = db.get(ReleaseEvalCase, payload.id)
        if not case:
            raise HTTPException(404, "评测题不存在")
        _document(db, case.document_key, user)
        key = case.document_key
        db.delete(case)
        audit(db, "quality_case_delete", None, f"{key}; case={payload.id}", actor=user)
    return ok()


async def _evaluate_side(case: ReleaseEvalCase, file_id: int, actor: User):
    documents = await find_context(case.question, user_id=actor.id, role=actor.role,
                                   evaluation_file_id=file_id)
    evidence = "\n".join(item["content"] for item in documents)
    fallback = evidence_reply(case.question, documents)
    if fallback:
        answer = fallback
    else:
        parts = []
        async for chunk in chat_chunks(answer_messages(case.question, documents), settings.customer_model, 0):
            parts.append(chunk)
            if sum(len(part) for part in parts) > 4000:
                raise RuntimeError("评测回答超过 4000 字符上限")
        answer = "".join(parts)
    if case.kind == "no_evidence":
        passed = not documents and answer == NO_EVIDENCE_REPLY
        checks = {"refused": passed}
    else:
        evidence_hits = {term: term.casefold() in evidence.casefold() for term in case.evidence_terms}
        answer_hits = {term: term.casefold() in answer.casefold() for term in case.answer_terms}
        passed = all(evidence_hits.values()) and all(answer_hits.values())
        checks = {"evidence": evidence_hits, "answer": answer_hits}
    return {"passed": passed, "checks": checks, "answer": answer,
            "evidence": [{"content": item["content"][:1600], "metadata": item.get("metadata") or {}}
                         for item in documents]}


@router.post("/run")
def run_release_eval(payload: CandidateRequest, user: User = Depends(admin_required)):
    require_llm(embedding=True)
    with db_session() as db:
        candidate, baseline, cases = _snapshot(db, payload.id, user)
        if candidate.review_status != "pending_review" or candidate.status != 2:
            raise HTTPException(409, "仅已处理完成、待审核版本可评测")
        if not cases:
            raise HTTPException(409, "请先为该资料标注评测题")
        if baseline and baseline.id == candidate.id:
            raise HTTPException(409, "当前发布版本无需发布评测")
        fingerprint = _fingerprint(candidate, baseline, cases)
        job_id = enqueue_quality(db, candidate.id, baseline.id if baseline else None,
                                 user.id, fingerprint, len(cases))
        audit(db, "quality_enqueue", candidate.id, f"job={job_id}; cases={len(cases)}", actor=user)
        job = db.get(ReleaseEvalJob, job_id)
        return ok({"jobId": str(job_id), "status": job.status, "total": len(cases)})


@router.post("/status")
def release_eval_status(payload: CandidateRequest, user: User = Depends(editor_required)):
    with db_session() as db:
        candidate, baseline, cases = _snapshot(db, payload.id, user)
        job = db.scalar(select(ReleaseEvalJob).where(ReleaseEvalJob.candidate_id == payload.id)
                        .order_by(ReleaseEvalJob.create_time.desc(), ReleaseEvalJob.id.desc()).limit(1))
        if not job:
            return ok(None)
        return ok({"jobId": str(job.id), "status": job.status, "cursor": job.cursor,
                   "total": job.total, "attempts": job.attempts, "maxAttempts": job.max_attempts,
                   "error": job.last_error, "current": job.fingerprint == _fingerprint(candidate, baseline, cases),
                   "reportId": str(job.report_id) if job.report_id else None})


@router.post("/latest")
def latest_release_eval(payload: CandidateRequest, user: User = Depends(editor_required)):
    with db_session() as db:
        candidate, baseline, cases = _snapshot(db, payload.id, user)
        run = db.scalar(select(ReleaseEvalRun).where(ReleaseEvalRun.candidate_id == payload.id)
                        .order_by(ReleaseEvalRun.create_time.desc(), ReleaseEvalRun.id.desc()).limit(1))
        if not run:
            return ok(None)
        current = run.fingerprint == _fingerprint(candidate, baseline, cases)
        return ok({"runId": str(run.id), "current": current, **run.report})


def require_passing_release_eval(db, candidate: MarkdownFile, baseline: MarkdownFile | None):
    cases = _cases(db, candidate.document_key)
    if not cases:
        raise HTTPException(409, "发布门禁：尚无标注评测题")
    run = db.scalar(select(ReleaseEvalRun).where(ReleaseEvalRun.candidate_id == candidate.id)
                    .order_by(ReleaseEvalRun.create_time.desc(), ReleaseEvalRun.id.desc()).limit(1))
    job = db.scalar(select(ReleaseEvalJob).where(ReleaseEvalJob.candidate_id == candidate.id)
                    .order_by(ReleaseEvalJob.create_time.desc(), ReleaseEvalJob.id.desc()).limit(1))
    if (not run or not job or job.status != "succeeded" or job.report_id != run.id
            or not run.passed or run.fingerprint != _fingerprint(candidate, baseline, cases)):
        raise HTTPException(409, "发布门禁：缺少当前版本与题库对应的通过报告，请先重新评测")
    return run.id
