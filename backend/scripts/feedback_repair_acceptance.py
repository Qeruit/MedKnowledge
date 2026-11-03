"""Real feedback -> repaired candidate -> real regression -> repaired closure.

Uses new NON-MEDICAL software fixtures and a temporary viewer account only.
Baseline approval is explicitly a local-demo synthetic test, never medical review.
The repaired v2 remains unpublished/pending_review; all evidence is retained.
No mocks, manual vector inserts, fabricated evaluation results or gate bypasses.
"""
import hashlib
import json
from pathlib import Path
import secrets
import time
from uuid import uuid4

import httpx
from sqlalchemy import select, text
from app.db import MarkdownFile, db_session

DESTINATION = Path('/app/storage/reports/feedback-repair-acceptance.json')


def existing_snapshot(ids=None):
    with db_session() as db:
        query = select(MarkdownFile.id).order_by(MarkdownFile.id)
        if ids is None:
            ids = list(db.scalars(query))
        rows = db.execute(text('''SELECT id,md5(row_to_json(f)::text) AS fingerprint
            FROM t_ai_customer_service_md_storage f WHERE id=ANY(:ids) ORDER BY id'''), {'ids':ids}).all()
        vectors = db.execute(text('''SELECT id::text,md5(content || metadata::text) AS fingerprint
            FROM t_vector_store WHERE metadata->>'mdStorageId'=ANY(:ids) ORDER BY id'''),
            {'ids':[str(i) for i in ids]}).all()
    digest = hashlib.sha256(json.dumps([list(map(list,rows)),list(map(list,vectors))],sort_keys=True).encode()).hexdigest()
    return ids,digest


def main():
    credentials=json.loads(Path('/app/.credentials/admin-bootstrap.json').read_text(encoding='utf-8'))
    suffix=uuid4().hex[:8]
    code='FBCODE'+suffix.upper()
    archive='ARCHIVE'+suffix.upper()
    question='本合成软件验收资料的任务识别码和归档标签分别是什么？'
    content=('# 非医疗反馈闭环软件验收资料\n\n## 任务识别码与归档标签\n'
             f'本合成软件验收资料的任务识别码为 {code}。\n'
             '本资料仅用于软件测试，不对应任何医疗器械，不涉及诊疗或患者。\n')
    original_ids,original_digest=existing_snapshot()
    evidence={'scope':'NON-MEDICAL synthetic fixtures only; real HTTP/CSRF/SSE/index/quality workers/model APIs; v2 not approved/published',
              'testId':suffix,'question':question,'expected':{'taskCode':code,'archiveLabel':archive},'checks':{},'stages':[]}
    checks=evidence['checks']
    viewer_info=None
    with httpx.Client(base_url='http://web',timeout=180,headers={'Origin':'http://127.0.0.1:8088'}) as admin, \
         httpx.Client(base_url='http://web',timeout=180,headers={'Origin':'http://127.0.0.1:8088'}) as viewer:
        def post(client,path,body):
            response=client.post('/api'+path,json=body)
            if response.status_code>=400:
                raise RuntimeError(f'{path}: HTTP {response.status_code}: {response.text[:500]}')
            value=response.json()
            if not value.get('success'): raise RuntimeError(f'{path}: {value.get("message")}')
            return value
        def data(path,body): return post(admin,path,body)['data']
        def login(client,username,password):
            result=post(client,'/auth/login',{'username':username,'password':password})['data']
            client.headers['X-CSRF-Token']=result['csrfToken']
        def stage(name):
            evidence['stages'].append(name)
            DESTINATION.parent.mkdir(parents=True,exist_ok=True)
            DESTINATION.write_text(json.dumps(evidence,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
            print(json.dumps({'stage':name,'testId':suffix},ensure_ascii=False),flush=True)
        def upload(name,body,base=None):
            response=admin.post('/api/customer-service/md/upload',files={'file':(name,body.encode(),'text/markdown')})
            response.raise_for_status()
            fid=response.json()['data']['id']
            evidence.setdefault('createdFileIds',[]).append(fid)
            deadline=time.monotonic()+180
            while time.monotonic()<deadline:
                with db_session() as db:
                    row=db.get(MarkdownFile,fid)
                    status=row.status
                if status==2: break
                if status==3: raise RuntimeError('Synthetic indexing failed')
                time.sleep(1)
            if status!=2: raise TimeoutError('Synthetic indexing timeout')
            payload={'id':fid,'revisionLabel':'NON-MEDICAL feedback repair acceptance '+('v2' if base else 'v1')}
            if base: payload['baseId']=base
            return fid,data('/customer-service/md/versions/register',payload)
        def evaluate(fid):
            job=data('/customer-service/md/quality/run',{'id':fid})
            evidence.setdefault('qualityJobIds',[]).append(job['jobId'])
            deadline=time.monotonic()+600
            while time.monotonic()<deadline:
                status=data('/customer-service/md/quality/status',{'id':fid})
                if status['status']=='succeeded':
                    result=data('/customer-service/md/quality/latest',{'id':fid})
                    if not result['current']: raise RuntimeError('Evaluation report unexpectedly stale')
                    return result
                if status['status'] in ('failed','cancelled','stale'):
                    raise RuntimeError(f'Synthetic evaluation did not complete: {status}')
                time.sleep(1)
            raise TimeoutError('Real quality worker timed out')
        def ask(fid):
            response=viewer.post('/api/customer-service/completion',json={'message':question,'fileId':fid})
            response.raise_for_status()
            events={}
            for frame in response.text.replace('\r\n','\n').split('\n\n'):
                kind='message'; parts=[]
                for line in frame.splitlines():
                    if line.startswith('event:'): kind=line[6:].strip()
                    if line.startswith('data:'): parts.append(line[5:].strip())
                if parts: events.setdefault(kind,[]).append(json.loads('\n'.join(parts)))
            if 'close' not in events or 'error' in events: raise RuntimeError('Synthetic SSE did not finish')
            return {'requestId':events['conversation'][0]['requestId'],
                    'answer':''.join(part.get('v','') for part in events.get('message',[])),
                    'sources':events.get('sources',[])}
        login(admin,credentials['username'],credentials['password'])
        try:
            policy=admin.get('/api/customer-service/md/versions/policy').json()['data']
            if not policy.get('allowSelfApproval'):
                raise RuntimeError('Local synthetic baseline approval exception is disabled; will not bypass policy')
            v1,version=upload('NON_MEDICAL_FEEDBACK_'+suffix+'_v1.md',content+'归档标签尚未录入，本版本不提供该字段。\n')
            evidence['baselineId']=v1; evidence['documentKey']=version['documentKey']
            for case in [
                {'question':'本合成软件验收资料的任务识别码是什么？','kind':'fact','evidenceTerms':[code],'answerTerms':[code]},
                {'question':'请写一个 Python 冒泡排序程序','kind':'no_evidence','evidenceTerms':[],'answerTerms':[]}]:
                data('/customer-service/md/quality/cases/add',{'documentKey':version['documentKey'],'critical':True,**case})
            data('/customer-service/md/versions/submit',{'id':v1})
            initial=evaluate(v1)
            evidence['initialBaselineReport']=initial
            assert initial['passed'], 'Synthetic baseline safety/code tests did not pass'
            data('/customer-service/md/versions/review',{'id':v1,'approve':True,'localDemoSelfApproval':True,
                'note':'仅非医疗合成软件验收：本地单管理员例外发布基线，用于复现资料缺字段的反馈闭环；不构成医学审批。'})
            password=secrets.token_urlsafe(24)
            viewer_info=data('/admin/users',{'username':'qa_feedback_'+suffix,'password':password,'role':'viewer'})
            evidence['testViewerId']=viewer_info['id']
            data('/customer-service/md/grants',{'id':v1,'userIds':[viewer_info['id']]})
            login(viewer,viewer_info['username'],password)
            stage('synthetic_baseline_published_and_viewer_authorized')
            original=ask(v1); evidence['originalConversation']=original
            checks['realIncompleteAnswer']=code in original['answer'] and archive not in original['answer']
            assert checks['realIncompleteAnswer'], 'Did not reproduce the expected missing-field answer'
            feedback=post(viewer,'/customer-service/feedback/submit',{'requestId':original['requestId'],
                'category':'incomplete','note':'非医疗合成软件验收：回答给出了任务识别码，但归档标签缺失，希望补齐资料后重新回答。'})['data']
            evidence['feedbackId']=feedback['id']
            detail=data('/customer-service/feedback/detail',{'id':feedback['id']})
            snapshot=detail['snapshot']; evidence['originalSnapshot']=snapshot
            text_evidence='\n'.join(doc['content'] for doc in snapshot['evidence']['documents'])
            checks['knowledgeGapConfirmedFromOriginalEvidence']=code in text_evidence and archive not in text_evidence and '尚未录入' in text_evidence
            checks['viewerCannotManage']=viewer.post('/api/customer-service/feedback/detail',json={'id':feedback['id']}).status_code==403
            assert checks['knowledgeGapConfirmedFromOriginalEvidence'] and checks['viewerCannotManage']
            feedback=data('/customer-service/feedback/update',{'id':feedback['id'],'revision':feedback['revision'],
                'status':'in_progress','diagnosis':'knowledge','note':'核对非医疗合成原文与当次证据：原文明确缺少归档标签，属于资料缺字段，并非检索遗漏；创建修复候选版补齐。'})
            stage('viewer_feedback_submitted_and_knowledge_gap_verified')
            v2,fixed_version=upload('NON_MEDICAL_FEEDBACK_'+suffix+'_v2.md',content+f'本合成软件验收资料的归档标签为 {archive}。\n',v1)
            evidence['candidateId']=v2
            checks['sameLogicalDocument']=fixed_version['documentKey']==version['documentKey'] and fixed_version['version']==2
            feedback=data('/customer-service/feedback/regression-case',{'id':feedback['id'],'revision':feedback['revision'],
                'targetFileId':v2,'question':question,'kind':'fact','evidenceTerms':[code,archive],'answerTerms':[code,archive]})
            evidence['regressionCaseId']=feedback['caseId']
            close_payload={'id':feedback['id'],'revision':feedback['revision'],'status':'closed','diagnosis':'knowledge',
                           'resolution':'repaired','note':'非医疗合成验收：补齐归档标签，需有效回归通过报告才可修复关闭。'}
            checks['missingReportRejected']=admin.post('/api/customer-service/feedback/update',json=close_payload).status_code==409
            checks['wrongBaselineReportRejected']=admin.post('/api/customer-service/feedback/update',json={**close_payload,'reportId':initial['runId']}).status_code==409
            assert checks['missingReportRejected'] and checks['wrongBaselineReportRejected']
            data('/customer-service/md/versions/submit',{'id':v2})
            stage('v2_indexed_regression_case_linked_and_invalid_closure_blocked')
            fixed=evaluate(v2); evidence['regressionReport']=fixed
            case=next(item for item in fixed['cases'] if item['case']['id']==feedback['caseId'])
            checks['baselineFailedRegression']=case['baseline']['passed'] is False
            checks['candidatePassedRegression']=case['candidate']['passed'] is True
            checks['allCasesPassedWithoutRegression']=fixed['passed'] and fixed['total']==3 and fixed['failed']==0 and fixed['regressions']==0
            checks['candidateAnswerContainsBothFacts']=all(term in case['candidate']['answer'] for term in [code,archive])
            assert all(checks.values()), 'One or more acceptance checks failed'
            stage('real_worker_report_old_failure_new_success_and_no_regressions')
            data('/customer-service/feedback/update',{**close_payload,'reportId':fixed['runId']})
            final=data('/customer-service/feedback/detail',{'id':feedback['id']})
            evidence['finalFeedback']=final
            mine=post(viewer,'/customer-service/feedback/mine',{'size':50})['data']
            checks['viewerSeesClosedResult']=any(item['id']==feedback['id'] and item['status']=='closed' and item['resolution']=='repaired' for item in mine)
            checks['closedWithExactVerifiedReport']=final['status']=='closed' and final['reportId']==fixed['runId'] and final['closureEvidence']['caseId']==feedback['caseId']
            checks['fullAuditTrail']=[event['action'] for event in final['events']]==['submit','update','regression_case','update']
            with db_session() as db:
                candidate=db.get(MarkdownFile,v2)
                checks['candidateNeverApprovedOrPublished']=not candidate.is_published and candidate.review_status=='pending_review' and candidate.reviewer_id is None
            checks['allPreexistingDocumentsAndVectorsUnchanged']=existing_snapshot(original_ids)[1]==original_digest
            assert all(checks.values()), 'Final acceptance verification failed'
            stage('repaired_closure_verified_and_all_preexisting_documents_unchanged')
        except Exception as exc:
            evidence['error']=str(exc)
            raise
        finally:
            viewer.post('/api/auth/logout',json={})
            if viewer_info:
                try:
                    data('/admin/users/update',{'id':viewer_info['id'],'role':'viewer','isActive':False})
                    checks['temporaryViewerDisabled']=True
                except Exception:
                    checks['temporaryViewerDisabled']=False
            admin.post('/api/auth/logout',json={})
            DESTINATION.parent.mkdir(parents=True,exist_ok=True)
            DESTINATION.write_text(json.dumps(evidence,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({'checks':checks,'feedbackId':evidence.get('feedbackId'),'baselineId':evidence.get('baselineId'),
                      'candidateId':evidence.get('candidateId'),'reportId':evidence.get('regressionReport',{}).get('runId'),
                      'artifact':str(DESTINATION)},ensure_ascii=False),flush=True)
    if not all(checks.values()): raise SystemExit(1)


if __name__=='__main__':
    main()
