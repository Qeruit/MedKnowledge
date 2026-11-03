"""Real SQL/API tests in an isolated disposable DB; no external model calls."""
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from app import main
from app.auth import current_user
from app.chat_history import capture_answer_evidence
from app.config import settings
from app.db import (AnswerFeedback, ChatConversation, ChatTurn, MarkdownFile, ReleaseEvalJob,
                    ReleaseEvalRun, User, db_session)
from app.release_quality import _cases, _fingerprint

pytestmark = pytest.mark.skipif(os.getenv('RUN_FEEDBACK_DB_TEST') != '1', reason='Use scripts.verify_feedback disposable DB')
PREFIX = '/customer-service/feedback/'


@pytest.fixture
def s():
    assert settings.database_name.startswith('agent_customer_service_feedback_')
    now = datetime.now()
    with db_session() as db:
        users = [User(username='feedback_'+uuid4().hex[:12], password_hash='not-a-login', role=role, is_active=True)
                 for role in ('admin', 'viewer', 'viewer', 'editor')]
        db.add_all(users); db.flush()
        key = uuid4()
        files = [MarkdownFile(original_file_name='synthetic.md', new_file_name=uuid4().hex+'.md',
            file_path='unused.md', file_size=20, status=2, owner_id=users[1].id, is_published=i==0,
            create_time=now, update_time=now, content_sha256='a'*64, document_key=key,
            version_number=i+1, review_status='approved' if i==0 else 'pending_review',
            embedding_model=settings.embedding_model, index_config={'test':1}, pdf_qa_revision=0)
            for i in range(2)]
        db.add_all(files); db.flush()
        conv = ChatConversation(id=uuid4(), user_id=users[1].id, file_id=files[0].id,
            created_at=now, updated_at=now, expires_at=now+timedelta(days=30))
        db.add(conv); db.flush()
        request_id = uuid4()
        db.add(ChatTurn(conversation_id=conv.id, request_id=request_id, question='synthetic question',
            answer='synthetic answer', sources=[{'fileId':files[0].id}], created_at=now,
            evidence_snapshot={'file':{'id':files[0].id,'fileName':'synthetic.md','version':1,'documentKey':str(key)},
                'documents':[{'content':'original exact evidence','metadata':{'mdStorageId':files[0].id}}],
                'model':'test-model','codeRevision':'original-code'}))
    identity = {'user':users[1]}
    main.app.dependency_overrides[current_user] = lambda: identity['user']
    try:
        yield SimpleNamespace(client=TestClient(main.app), users=users, identity=identity,
                              files=files, conv=conv.id, request_id=request_id)
    finally:
        main.app.dependency_overrides.pop(current_user, None)


def post(s, path, **body):
    return s.client.post(PREFIX+path, json=body)


def submit(s):
    res=post(s,'submit',requestId=str(s.request_id),category='incomplete',note='synthetic experience')
    assert res.status_code==200, res.text
    return res.json()['data']


def manage(s, row, **body):
    s.identity['user']=s.users[0]
    return post(s,'update',id=row['id'],revision=row['revision'],status='in_progress',
                diagnosis='retrieval',note='Synthetic manager verification',**body)


def linked(s):
    row=submit(s)
    row=manage(s,row).json()['data']
    res=post(s,'regression-case',id=row['id'],revision=row['revision'],targetFileId=s.files[1].id,
        question='synthetic regression question',kind='fact',evidenceTerms=['original'],answerTerms=['answer'])
    assert res.status_code==200, res.text
    return res.json()['data']


def report(s,row, *,passed=True, stale=False, missing=False, wrong_target=False):
    now=datetime.now()
    with db_session() as db:
        target=db.get(MarkdownFile,s.files[1].id)
        baseline=db.get(MarkdownFile,s.files[0].id)
        fingerprint=_fingerprint(target,baseline,_cases(db,target.document_key))
        run=ReleaseEvalRun(id=uuid4(),candidate_id=baseline.id if wrong_target else target.id,
            baseline_id=baseline.id,fingerprint='0'*64 if stale else fingerprint,passed=passed,
            report={'cases':[] if missing else [{'case':{'id':row['caseId']},'candidate':{'passed':True}}]},
            actor_id=s.users[0].id,create_time=now)
        db.add(run)
        db.add(ReleaseEvalJob(id=uuid4(),candidate_id=target.id,baseline_id=baseline.id,actor_id=s.users[0].id,
            fingerprint=fingerprint,status='succeeded',cursor=1,total=1,results=[],attempts=1,max_attempts=3,
            available_at=now,report_id=run.id,create_time=now,update_time=now))
    return str(run.id)


def close(s,row,rid):
    return post(s,'update',id=row['id'],revision=row['revision'],status='closed',diagnosis='retrieval',
                note='Synthetic repaired regression verified',resolution='repaired',reportId=rid)


def test_snapshot_is_original_idempotent_and_survives_conversation_deletion(s):
    row=submit(s)
    assert submit(s)['id']==row['id']
    # A repeated report must not replace the original explanation or snapshot.
    assert post(s,'submit',requestId=str(s.request_id),category='other',note='changed').json()['data']['category']=='incomplete'
    with db_session() as db:
        db.execute(delete(ChatConversation).where(ChatConversation.id==s.conv))
    s.identity['user']=s.users[0]
    detail=post(s,'detail',id=row['id']).json()['data']
    assert detail['snapshot']['evidence']['documents'][0]['content']=='original exact evidence'
    assert detail['snapshot']['answer']=='synthetic answer'
    assert len(detail['events'])==1


def test_cannot_forge_request_or_report_another_users_answer(s):
    assert post(s,'submit',requestId=str(uuid4()),category='other').status_code==404
    s.identity['user']=s.users[2]
    assert post(s,'submit',requestId=str(s.request_id),category='other').status_code==404


def test_feedback_management_permissions(s):
    row=submit(s)
    assert post(s,'list').status_code==403
    assert post(s,'detail',id=row['id']).status_code==403
    assert post(s,'mine').json()['total']==1
    s.identity['user']=s.users[3]
    assert post(s,'list').json()['total']==0
    assert post(s,'detail',id=row['id']).status_code==404
    assert post(s,'regression-case',id=row['id'],revision=1,targetFileId=s.files[1].id,
        question='test',kind='no_evidence').status_code==403


def test_revoked_source_access_blocks_new_submission(s):
    with db_session() as db:
        db.get(MarkdownFile,s.files[0].id).is_published=False
    assert post(s,'submit',requestId=str(s.request_id),category='other').status_code==404


def test_legacy_missing_snapshot_is_explicit_not_retrieved(s):
    with db_session() as db:
        db.scalar(select(ChatTurn).where(ChatTurn.request_id==s.request_id)).evidence_snapshot=None
    row=submit(s); s.identity['user']=s.users[0]
    snap=post(s,'detail',id=row['id']).json()['data']['snapshot']
    assert snap['evidence'] is None and not snap['evidenceAvailable']
    assert '未重新检索' in snap['evidenceNotice']


def test_revision_conflict_nonfix_closure_and_reopen_audit(s):
    row=submit(s); updated=manage(s,row).json()['data']
    assert manage(s,row).status_code==409
    res=post(s,'update',id=row['id'],revision=updated['revision'],status='closed',diagnosis='interaction',
             resolution='explained',note='Synthetic explanation only, not a factual correction')
    assert res.status_code==200,res.text
    closed=res.json()['data']
    res=post(s,'update',id=row['id'],revision=closed['revision'],status='in_progress',note='Synthetic reopen reason')
    assert res.status_code==200
    detail=post(s,'detail',id=row['id']).json()['data']
    assert len(detail['events'])==4
    assert detail['closureEvidence'] is None


def test_cannot_close_as_repaired_without_regression(s):
    row=submit(s); row=manage(s,row).json()['data']
    assert close(s,row,str(uuid4())).status_code==409


def test_regression_case_same_document_candidate_and_no_duplicates(s):
    row=submit(s); row=manage(s,row).json()['data']
    body=dict(id=row['id'],revision=row['revision'],question='test regression',kind='no_evidence')
    assert post(s,'regression-case',targetFileId=s.files[0].id,**body).status_code==409
    with db_session() as db:
        candidate=db.get(MarkdownFile,s.files[1].id); original=candidate.document_key; candidate.document_key=uuid4()
    assert post(s,'regression-case',targetFileId=s.files[1].id,**body).status_code==409
    with db_session() as db:
        db.get(MarkdownFile,s.files[1].id).document_key=original
    res=post(s,'regression-case',targetFileId=s.files[1].id,**body)
    assert res.status_code==200,res.text
    body['revision']=res.json()['data']['revision']
    assert post(s,'regression-case',targetFileId=s.files[1].id,**body).status_code==409


@pytest.mark.parametrize('fault',['failed','stale','missing','wrong_target'])
def test_repaired_closure_rejects_invalid_report(s,fault):
    row=linked(s)
    rid=report(s,row,passed=fault!='failed',stale=fault=='stale',missing=fault=='missing',wrong_target=fault=='wrong_target')
    assert close(s,row,rid).status_code==409


def test_repaired_closure_retains_verified_case_and_report(s):
    row=linked(s); rid=report(s,row)
    res=close(s,row,rid)
    assert res.status_code==200,res.text
    detail=post(s,'detail',id=row['id']).json()['data']
    assert detail['status']=='closed' and detail['resolution']=='repaired'
    assert detail['closureEvidence']['reportId']==rid
    assert detail['closureEvidence']['caseId']==row['caseId']


def test_capture_uses_exact_selected_evidence_and_configuration(s):
    docs=[{'content':'exact selected context','metadata':{'pdfPage':7}}]
    snapshot=capture_answer_evidence(s.files[0].id,docs,'retrieval question')
    docs[0]['content']='changed later'
    assert snapshot['documents'][0]['content']=='exact selected context'
    assert snapshot['retrievalQuestion']=='retrieval question'
    assert snapshot['file']['id']==s.files[0].id and snapshot['codeRevision']


def test_actual_sse_persists_original_evidence_and_exposes_request_id(s,monkeypatch):
    monkeypatch.setattr(main,'require_llm',lambda **_:None)
    monkeypatch.setattr(main,'begin_request',lambda *_:uuid4())
    monkeypatch.setattr(main,'reserve_prompt',lambda *_:None)
    monkeypatch.setattr(main,'finish_request',lambda *_,**__:True)
    monkeypatch.setattr(main,'evidence_reply',lambda *_:None)
    async def context(*_,**__):
        return [{'content':'synthetic original exact selected context','metadata':{'mdStorageId':s.files[0].id}}]
    async def chunks(*_):
        yield 'synthetic answer'
    monkeypatch.setattr(main,'find_context',context)
    monkeypatch.setattr(main,'chat_chunks',chunks)
    response=s.client.post('/customer-service/completion',json={'message':'synthetic question','fileId':s.files[0].id})
    assert response.status_code==200 and 'event: close' in response.text
    import json
    scope=json.loads(response.text.split('event: conversation\n')[1].split('data: ')[1].split('\n')[0])
    assert scope['requestId']
    with db_session() as db:
        turn=db.scalar(select(ChatTurn).where(ChatTurn.request_id==scope['requestId']))
        assert turn.answer=='synthetic answer'
        assert turn.evidence_snapshot['documents'][0]['content']=='synthetic original exact selected context'
    result=post(s,'submit',requestId=scope['requestId'],category='not_helpful')
    assert result.status_code==200,result.text
