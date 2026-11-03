"""User experience feedback, immutable answer evidence and manager-led regression."""
import copy
from datetime import timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select

from app.access import readable_scope, require_file_manager
from app.audit import audit
from app.chat_history import utc_now
from app.auth import current_user, editor_required, admin_required
from app.contracts import ok, page
from app.db import (AnswerFeedback, FeedbackEvent, ChatTurn, ChatConversation, MarkdownFile,
                    ReleaseEvalCase, ReleaseEvalRun, ReleaseEvalJob, User, db_session)
from app.release_quality import CaseRequest, MAX_CASES, _cases, _fingerprint

router = APIRouter(prefix="/customer-service/feedback", tags=["answer-feedback"])
Status = Literal['pending','in_progress','pending_regression','closed']
Diagnosis = Literal['unknown','knowledge','parse','retrieval','generation','interaction','not_issue']
Category = Literal['not_helpful','incomplete','contradictory','unclear','citation_unavailable','other']
Resolution = Literal['repaired','explained','not_issue','duplicate']


class Submit(BaseModel):
    requestId: UUID
    category: Category
    note: str = Field(default="", max_length=1000)


class Query(BaseModel):
    current: int = Field(default=1, ge=1)
    size: int = Field(default=10, ge=1, le=50)
    status: Status | None = None


class Identifier(BaseModel):
    id: int = Field(gt=0)


class Update(Identifier):
    revision: int = Field(ge=1)
    status: Status
    diagnosis: Diagnosis = 'unknown'
    note: str = Field(min_length=5, max_length=2000)
    resolution: Resolution | None = None
    reportId: UUID | None = None


class Convert(Identifier):
    revision: int = Field(ge=1)
    targetFileId: int = Field(gt=0)
    question: str = Field(min_length=2, max_length=500)
    kind: Literal['fact','no_evidence'] = 'fact'
    evidenceTerms: list[str] = Field(default_factory=list, max_length=10)
    answerTerms: list[str] = Field(default_factory=list, max_length=10)


def summary(row):
    return {"id":row.id,"requestId":str(row.request_id),"fileId":row.file_id,
        "fileName":row.snapshot.get("file",{}).get("fileName"),"category":row.category,
        "status":row.status,"diagnosis":row.diagnosis,"revision":row.revision,
        "caseId":row.case_id,"regressionFileId":row.regression_file_id,
        "reportId":str(row.report_id) if row.report_id else None,
        "question":row.snapshot.get("question",""),"userNote":row.user_note,
        "resolution":row.resolution,"resolutionNote":row.resolution_note,
        "createdAt":row.created_at.isoformat()+"Z","updatedAt":row.updated_at.isoformat()+"Z"}


def get_managed(db, identifier, user, *, revision=None):
    row=db.scalar(select(AnswerFeedback).where(AnswerFeedback.id==identifier).with_for_update())
    if not row: raise HTTPException(404,"反馈不存在或无管理权限")
    file=db.get(MarkdownFile,row.file_id) if row.file_id else None
    if not (user.role=='admin' and file is None): require_file_manager(file,user)
    if revision is not None and revision!=row.revision:
        raise HTTPException(409,"反馈已被其他操作更新，请刷新后再处理")
    return row


def event(db,row,user,action,detail):
    db.add(FeedbackEvent(feedback_id=row.id,actor_id=user.id,action=action,
                         detail=detail,created_at=utc_now()))
    audit(db,"feedback_"+action,row.file_id,f"feedback={row.id}; revision={row.revision}",actor=user)


@router.post('/submit')
def submit(payload:Submit,user:User=Depends(current_user)):
    with db_session() as db:
        db.scalar(select(User).where(User.id==user.id).with_for_update())
        existing=db.scalar(select(AnswerFeedback).where(AnswerFeedback.user_id==user.id,
                                                        AnswerFeedback.request_id==payload.requestId))
        if existing: return ok(summary(existing))
        pair=db.execute(select(ChatTurn,ChatConversation).join(ChatConversation,
            ChatConversation.id==ChatTurn.conversation_id).where(ChatTurn.request_id==payload.requestId,
            ChatConversation.user_id==user.id)).first()
        if not pair: raise HTTPException(404,"回答不存在、未完成或不属于当前用户")
        turn,conversation=pair
        file=db.scalar(select(MarkdownFile).where(MarkdownFile.id==conversation.file_id,readable_scope(user)))
        if not file: raise HTTPException(404,"资料不存在、未发布或无访问权限")
        count=db.scalar(select(func.count()).select_from(AnswerFeedback).where(
            AnswerFeedback.user_id==user.id,AnswerFeedback.created_at>utc_now()-timedelta(days=1)))
        if count>=50: raise HTTPException(429,"每天最多提交50条新反馈")
        evidence=copy.deepcopy(turn.evidence_snapshot)
        identity=(evidence or {}).get("file") or {"id":file.id,"fileName":file.original_file_name,
            "version":file.version_number,"documentKey":str(file.document_key) if file.document_key else None}
        snapshot={"question":turn.question,"answer":turn.answer,"sources":copy.deepcopy(turn.sources or []),
            "file":identity,"evidence":evidence,"evidenceAvailable":evidence is not None,
            "evidenceNotice":None if evidence is not None else "历史回答未保存当次证据；未重新检索补造现场"}
        row=AnswerFeedback(user_id=user.id,request_id=payload.requestId,file_id=file.id,
            category=payload.category,user_note=payload.note.strip(),snapshot=snapshot,status='pending',
            diagnosis='unknown',revision=1,created_at=utc_now(),updated_at=utc_now())
        db.add(row); db.flush(); event(db,row,user,'submit',{"category":payload.category})
        return ok(summary(row))


def listing(payload,user,mine):
    with db_session() as db:
        clause=AnswerFeedback.user_id==user.id if mine else (True if user.role=='admin' else
            AnswerFeedback.file_id.in_(select(MarkdownFile.id).where(MarkdownFile.owner_id==user.id)))
        conditions=[clause]+([AnswerFeedback.status==payload.status] if payload.status else [])
        total=db.scalar(select(func.count()).select_from(AnswerFeedback).where(*conditions))
        rows=db.scalars(select(AnswerFeedback).where(*conditions).order_by(AnswerFeedback.updated_at.desc(),
            AnswerFeedback.id.desc()).offset((payload.current-1)*payload.size).limit(payload.size)).all()
        values=[summary(r) for r in rows]
        if mine:
            for value in values:
                value.pop('diagnosis',None); value.pop('caseId',None); value.pop('reportId',None)
        return page(values,total,payload.current,payload.size)


@router.post('/mine')
def mine(payload:Query,user:User=Depends(current_user)):
    return listing(payload,user,True)


@router.post('/list')
def manager_list(payload:Query,user:User=Depends(editor_required)):
    return listing(payload,user,False)


@router.post('/detail')
def detail(payload:Identifier,user:User=Depends(editor_required)):
    with db_session() as db:
        row=get_managed(db,payload.id,user)
        events=db.scalars(select(FeedbackEvent).where(FeedbackEvent.feedback_id==row.id)
                         .order_by(FeedbackEvent.id)).all()
        return ok(summary(row)|{"snapshot":row.snapshot,"closureEvidence":row.closure_evidence,
            "events":[{"action":e.action,"actorId":e.actor_id,"detail":e.detail,
                       "createdAt":e.created_at.isoformat()+"Z"} for e in events]})


def validate_regression(db,row,report_id,user):
    if not row.case_id or not row.regression_file_id or not report_id:
        raise HTTPException(409,"修复关闭需先关联回归题、目标版本及通过报告")
    target=db.scalar(select(MarkdownFile).where(MarkdownFile.id==row.regression_file_id).with_for_update())
    require_file_manager(target,user)
    run=db.get(ReleaseEvalRun,report_id)
    cases=_cases(db,target.document_key)
    baseline=db.scalar(select(MarkdownFile).where(MarkdownFile.document_key==target.document_key,
                                                  MarkdownFile.is_published.is_(True)))
    job=db.scalar(select(ReleaseEvalJob).where(ReleaseEvalJob.candidate_id==target.id)
                  .order_by(ReleaseEvalJob.create_time.desc(),ReleaseEvalJob.id.desc()).limit(1))
    if (not run or run.candidate_id!=target.id or not run.passed or not job or job.status!='succeeded'
        or job.report_id!=run.id or job.fingerprint!=run.fingerprint or run.fingerprint!=_fingerprint(target,baseline,cases)
        or not any(c.id==row.case_id for c in cases)):
        raise HTTPException(409,"报告失败、过期、不是目标版本或未完成；请在发布修复版之前重新回归")
    matched=next((item for item in run.report.get('cases',[]) if item.get('case',{}).get('id')==row.case_id),None)
    if not matched or not matched.get('candidate',{}).get('passed'):
        raise HTTPException(409,"报告未包含本反馈关联题的通过结果")
    return {"reportId":str(run.id),"candidateId":target.id,"caseId":row.case_id,
            "fingerprint":run.fingerprint,"verifiedAt":utc_now().isoformat()+"Z"}


@router.post('/update')
def update(payload:Update,user:User=Depends(editor_required)):
    if len(payload.note.strip())<5: raise HTTPException(422,"处理说明至少5字")
    with db_session() as db:
        row=get_managed(db,payload.id,user,revision=payload.revision)
        allowed={'pending':{'in_progress','closed'},'in_progress':{'in_progress','pending_regression','closed'},
                 'pending_regression':{'in_progress','pending_regression','closed'},'closed':{'in_progress'}}
        if payload.status not in allowed[row.status]: raise HTTPException(409,"不允许的反馈状态流转")
        evidence=None
        if payload.status=='closed':
            if not payload.resolution: raise HTTPException(422,"关闭必须选择处理结果")
            if payload.resolution=='repaired':
                if payload.diagnosis in ('unknown','not_issue'): raise HTTPException(422,"修复关闭须明确问题分类")
                if row.status!='pending_regression': raise HTTPException(409,"修复关闭须先进入待回归状态")
                evidence=validate_regression(db,row,payload.reportId,user)
            elif payload.reportId: raise HTTPException(422,"非修复关闭不应关联评测报告")
        elif payload.resolution or payload.reportId:
            raise HTTPException(422,"仅关闭时填写处理结果和报告")
        previous=row.status
        row.status=payload.status; row.diagnosis=payload.diagnosis
        row.resolution=payload.resolution; row.resolution_note=payload.note.strip()
        row.report_id=payload.reportId; row.closure_evidence=evidence
        row.revision+=1; row.updated_at=utc_now()
        event(db,row,user,'update',{"from":previous,"to":row.status,"diagnosis":row.diagnosis,
            "resolution":row.resolution,"note":row.resolution_note,"closureEvidence":evidence})
        return ok(summary(row))


@router.post('/regression-case')
def regression_case(payload:Convert,user:User=Depends(admin_required)):
    with db_session() as db:
        row=get_managed(db,payload.id,user,revision=payload.revision)
        if row.status not in ('in_progress','pending_regression'):
            raise HTTPException(409,"请先核实反馈并进入处理中")
        if row.diagnosis in ('unknown','not_issue'): raise HTTPException(409,"请先确认问题分类，不能直接把用户反馈视为错误")
        target=db.scalar(select(MarkdownFile).where(MarkdownFile.id==payload.targetFileId).with_for_update())
        require_file_manager(target,user)
        expected=row.snapshot.get('file',{}).get('documentKey')
        if not expected or str(target.document_key)!=expected: raise HTTPException(409,"回归目标必须属于反馈的同一逻辑资料")
        if target.status!=2 or target.is_published or target.review_status not in ('draft','pending_review','rejected'):
            raise HTTPException(409,"请选择已处理、未发布的修复候选版本；已发布资料应先创建新版本")
        if row.case_id:
            existing=db.get(ReleaseEvalCase,row.case_id)
            if existing: raise HTTPException(409,"已关联回归题，请使用现有题并核对题目；不要重复添加")
        try:
            validated=CaseRequest(documentKey=target.document_key,question=payload.question,kind=payload.kind,
                evidenceTerms=payload.evidenceTerms,answerTerms=payload.answerTerms,critical=True)
        except ValidationError as exc:
            raise HTTPException(422,"请核对评测题类型、证据短语和答案短语；拒答题不填写事实短语") from exc
        if len(_cases(db,target.document_key))>=MAX_CASES: raise HTTPException(409,"题库已达上限，请先整理评测题")
        case=ReleaseEvalCase(document_key=target.document_key,question=validated.question,kind=validated.kind,
            evidence_terms=validated.evidenceTerms,answer_terms=validated.answerTerms,critical=True,create_time=utc_now())
        db.add(case); db.flush()
        row.case_id=case.id; row.regression_file_id=target.id; row.status='pending_regression'
        row.revision+=1; row.updated_at=utc_now()
        event(db,row,user,'regression_case',{"caseId":case.id,"targetFileId":target.id})
        return ok(summary(row))
