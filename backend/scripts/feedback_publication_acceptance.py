"""Publish the exact NON-MEDICAL repaired fixture, then verify as an ordinary viewer.

Uses the normal reviewed-version endpoint and its release gate. No direct SQL writes,
report fabrication, policy changes, or approvals of medical documents.
"""
import hashlib
import json
from pathlib import Path
import secrets
from uuid import uuid4

import httpx
from sqlalchemy import select
from app.db import MarkdownFile, FileGrant, ChatTurn, User, db_session
from scripts.feedback_repair_acceptance import existing_snapshot

DESTINATION = Path('/app/storage/reports/feedback-publication-acceptance.json')
REPAIR = Path('/app/storage/reports/feedback-repair-acceptance.json')


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def main():
    repair=json.loads(REPAIR.read_text(encoding='utf-8'))
    baseline_id,candidate_id=repair['baselineId'],repair['candidateId']
    feedback_id=repair['feedbackId']
    assert (baseline_id,candidate_id,feedback_id)==(14,15,2), 'Unexpected fixture: refuse automatic target changes'
    with db_session() as db:
        baseline,candidate=db.get(MarkdownFile,baseline_id),db.get(MarkdownFile,candidate_id)
        assert baseline.original_file_name=='NON_MEDICAL_FEEDBACK_338512d2_v1.md'
        assert candidate.original_file_name=='NON_MEDICAL_FEEDBACK_338512d2_v2.md'
        assert baseline.document_key==candidate.document_key and str(candidate.document_key)==repair['documentKey']
        assert baseline.is_published and baseline.review_status=='approved'
        assert candidate.status==2 and not candidate.is_published and candidate.review_status=='pending_review'
        other_ids=list(db.scalars(select(MarkdownFile.id).where(MarkdownFile.id.not_in([baseline_id,candidate_id]))))
    _,other_digest=existing_snapshot(other_ids)
    checks={}
    proof={'scope':'User-authorized publication of exact NON-MEDICAL fixture #15 only; real viewer HTTP/CSRF/SSE/model verification',
           'baselineId':baseline_id,'candidateId':candidate_id,'feedbackId':feedback_id,'checks':checks,'stages':[]}
    credentials=json.loads(Path('/app/.credentials/admin-bootstrap.json').read_text(encoding='utf-8'))
    viewer_info=None
    with httpx.Client(base_url='http://web',timeout=180,headers={'Origin':'http://127.0.0.1:8088'}) as admin, \
         httpx.Client(base_url='http://web',timeout=180,headers={'Origin':'http://127.0.0.1:8088'}) as viewer:
        def post(client,path,body):
            response=client.post('/api'+path,json=body)
            if response.status_code>=400:
                raise RuntimeError(f'{path}: HTTP {response.status_code}: {response.text[:500]}')
            value=response.json()
            if not value.get('success'): raise RuntimeError(f'{path}: {value.get("message")}')
            return value['data']
        def get(client,path):
            response=client.get('/api'+path); response.raise_for_status()
            return response.json()['data']
        def login(client,username,password):
            info=post(client,'/auth/login',{'username':username,'password':password})
            client.headers['X-CSRF-Token']=info['csrfToken']
        def stage(name):
            proof['stages'].append(name)
            DESTINATION.parent.mkdir(parents=True,exist_ok=True)
            DESTINATION.write_text(json.dumps(proof,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
            print(json.dumps({'stage':name},ensure_ascii=False),flush=True)
        def ask(file_id):
            response=viewer.post('/api/customer-service/completion',json={'message':repair['question'],'fileId':file_id})
            response.raise_for_status()
            events={}
            for frame in response.text.replace('\r\n','\n').split('\n\n'):
                kind='message'; parts=[]
                for line in frame.splitlines():
                    if line.startswith('event:'): kind=line[6:].strip()
                    if line.startswith('data:'): parts.append(line[5:].strip())
                if parts: events.setdefault(kind,[]).append(json.loads('\n'.join(parts)))
            if 'close' not in events or 'error' in events: raise RuntimeError('Real viewer SSE failed')
            return {'scope':events['conversation'][0],
                'answer':''.join(part.get('v','') for part in events.get('message',[])),
                'sources':[item for event in events.get('sources',[]) for item in event.get('items',[])]}
        login(admin,credentials['username'],credentials['password'])
        try:
            policy=get(admin,'/customer-service/md/versions/policy')
            if not policy.get('allowSelfApproval'):
                raise RuntimeError('Local synthetic approval exception disabled; will not bypass policy')
            report=post(admin,'/customer-service/md/quality/latest',{'id':candidate_id})
            checks['passingReportCurrentBeforePublication']=report['current'] and report['passed'] and report['runId']==repair['regressionReport']['runId']
            original_feedback=post(admin,'/customer-service/feedback/detail',{'id':feedback_id})
            checks['feedbackAlreadyClosedWithCorrectReport']=original_feedback['status']=='closed' and original_feedback['resolution']=='repaired' and original_feedback['reportId']==report['runId']
            assert all(checks.values()), 'Prerequisites are not satisfied'
            proof['releaseGateReportId']=report['runId']
            proof['originalSnapshotSha256']=digest(original_feedback['snapshot'])
            password=secrets.token_urlsafe(24)
            viewer_info=post(admin,'/admin/users',{'username':'qa_publish_'+uuid4().hex[:8],'password':password,'role':'viewer'})
            proof['temporaryViewerId']=viewer_info['id']
            with db_session() as db:
                all_old_grants=list(db.scalars(select(FileGrant.user_id).where(FileGrant.file_id==baseline_id)))
                old_grants=list(db.scalars(select(FileGrant.user_id).join(User,User.id==FileGrant.user_id)
                    .where(FileGrant.file_id==baseline_id,User.is_active.is_(True))))
            proof['inactiveSyntheticGrantsRemoved']=sorted(set(all_old_grants)-set(old_grants))
            grants=sorted(set(old_grants+[viewer_info['id']]))
            post(admin,'/customer-service/md/grants',{'id':baseline_id,'userIds':grants})
            login(viewer,viewer_info['username'],password)
            available=get(viewer,'/customer-service/md/available')
            checks['beforeOnlyV1Visible']=[item['id'] for item in available]==[baseline_id]
            checks['unpublishedV2QueryRejected']=viewer.post('/api/customer-service/completion',json={'message':repair['question'],'fileId':candidate_id}).status_code==404
            old_answer=ask(baseline_id); proof['beforePublication']=old_answer
            checks['beforeAnswerMissingArchive']=repair['expected']['taskCode'] in old_answer['answer'] and repair['expected']['archiveLabel'] not in old_answer['answer']
            assert all(checks.values())
            stage('prerequisites_and_real_viewer_old_version_verified')
            post(admin,'/customer-service/md/versions/review',{'id':candidate_id,'approve':True,'localDemoSelfApproval':True,
                'note':'用户授权的非医疗合成反馈闭环发布验收：已核对3题通过报告，仅批准软件测试资料v2；本地单管理员例外，不构成医学审核。'})
            stage('synthetic_v2_published_through_existing_release_gate')
            available=get(viewer,'/customer-service/md/available'); proof['afterAvailable']=available
            checks['afterOnlyV2Visible']=[item['id'] for item in available]==[candidate_id]
            with db_session() as db:
                baseline,candidate=db.get(MarkdownFile,baseline_id),db.get(MarkdownFile,candidate_id)
                inherited=list(db.scalars(select(FileGrant.user_id).where(FileGrant.file_id==candidate_id)))
                checks['grantsInheritedExactly']=sorted(inherited)==grants
                checks['versionSwitchedAtomically']=candidate.is_published and candidate.review_status=='approved' and not baseline.is_published and baseline.review_status=='superseded'
            result=ask(candidate_id); proof['afterPublication']=result
            checks['realViewerAnswerContainsBothFacts']=all(term in result['answer'] for term in repair['expected'].values())
            checks['sourceCardsPointToV2']=bool(result['sources']) and all(source['version']==2 and source['fileName']=='NON_MEDICAL_FEEDBACK_338512d2_v2.md' and source['documentKey']==repair['documentKey'] for source in result['sources'])
            with db_session() as db:
                turn=db.scalar(select(ChatTurn).where(ChatTurn.request_id==result['scope']['requestId']))
                snapshot=turn.evidence_snapshot
                proof['actualV2Evidence']=snapshot
                checks['actualEvidenceOnlyFromV2']=snapshot['file']['id']==candidate_id and bool(snapshot['documents']) and all(doc['metadata']['mdStorageId']==candidate_id for doc in snapshot['documents'])
            old_request=viewer.post('/api/customer-service/completion',json={'message':repair['question'],'fileId':baseline_id})
            proof['oldVersionQueryStatus']=old_request.status_code
            checks['oldVersionQueryRejected']=old_request.status_code==404
            checks['oldConversationReadRejected']=viewer.get('/api/customer-service/conversations/'+old_answer['scope']['id']).status_code==404
            checks['oldConversationCannotCrossVersion']=viewer.post('/api/customer-service/completion',json={
                'message':repair['question'],'fileId':candidate_id,'conversationId':old_answer['scope']['id']}).status_code==409
            final_feedback=post(admin,'/customer-service/feedback/detail',{'id':feedback_id})
            proof['finalFeedback']=final_feedback
            checks['oldFeedbackEvidenceUnchanged']=digest(final_feedback)==digest(original_feedback)
            checks['unrelatedDocumentsAndVectorsUnchanged']=existing_snapshot(other_ids)[1]==other_digest
            assert all(checks.values()), 'Publication acceptance failed'
            stage('new_answer_authorization_old_version_isolation_and_old_evidence_verified')
        except Exception as exc:
            proof['error']=str(exc)
            raise
        finally:
            viewer.post('/api/auth/logout',json={})
            if viewer_info:
                try:
                    post(admin,'/admin/users/update',{'id':viewer_info['id'],'role':'viewer','isActive':False})
                    checks['temporaryViewerDisabled']=True
                except Exception:
                    checks['temporaryViewerDisabled']=False
            admin.post('/api/auth/logout',json={})
            DESTINATION.parent.mkdir(parents=True,exist_ok=True)
            DESTINATION.write_text(json.dumps(proof,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({'checks':checks,'feedbackId':feedback_id,'publishedFileId':candidate_id,'artifact':str(DESTINATION)},ensure_ascii=False),flush=True)
    if not all(checks.values()): raise SystemExit(1)


if __name__=='__main__':
    main()
