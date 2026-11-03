"""Authenticated feedback acceptance on existing NON-MEDICAL published fixture only.

Creates a synthetic conversation and feedback, closes as 'explained' (not repaired).
Never edits documents, creates assessment reports or grants approval/publication.
"""
import json
from pathlib import Path
import httpx
from sqlalchemy import select
from app.db import MarkdownFile, db_session


def main():
    with db_session() as db:
        file=db.scalar(select(MarkdownFile).where(MarkdownFile.is_published.is_(True),
            MarkdownFile.original_file_name=='NON_MEDICAL_AGENT_CLOSED_LOOP_20261020.md'))
        if file is None:
            raise RuntimeError('Existing published NON-MEDICAL acceptance fixture missing; no medical fallback allowed')
        fid=file.id
        before=(file.review_status,file.is_published,file.content_sha256,file.update_time)
    credentials=json.loads(Path('/app/.credentials/admin-bootstrap.json').read_text(encoding='utf-8'))
    checks={}
    with httpx.Client(base_url='http://web',timeout=180,headers={'Origin':'http://127.0.0.1:8088'}) as client:
        def post(path,body):
            response=client.post('/api'+path,json=body)
            response.raise_for_status()
            return response.json()
        login=post('/auth/login',{'username':credentials['username'],'password':credentials['password']})['data']
        client.headers['X-CSRF-Token']=login['csrfToken']
        try:
            response=client.post('/api/customer-service/completion',json={'message':'本合成验收资料的任务识别码是什么？','fileId':fid})
            response.raise_for_status()
            events={}
            for frame in response.text.replace('\r\n','\n').split('\n\n'):
                name='message'; data=[]
                for line in frame.splitlines():
                    if line.startswith('event:'): name=line[6:].strip()
                    if line.startswith('data:'): data.append(line[5:].strip())
                if data: events.setdefault(name,[]).append(json.loads('\n'.join(data)))
            scope=events['conversation'][0]
            checks['sseCompleted']='close' in events and 'error' not in events
            assert checks['sseCompleted'], 'Synthetic answer did not complete'
            payload={'requestId':scope['requestId'],'category':'other',
                     'note':'非医疗合成软件验收：验证反馈闭环，不表示真实回答有误。'}
            row=post('/customer-service/feedback/submit',payload)['data']
            checks['duplicateSubmissionIsIdempotent']=post('/customer-service/feedback/submit',payload)['data']['id']==row['id']
            detail=post('/customer-service/feedback/detail',{'id':row['id']})['data']
            evidence=detail['snapshot']['evidence']
            checks['originalEvidenceSaved']=bool(detail['snapshot']['evidenceAvailable'] and evidence['documents'] and evidence['codeRevision'])
            checks['originalRequestMatched']=detail['requestId']==scope['requestId']
            checks['mineVisible']=any(item['id']==row['id'] for item in post('/customer-service/feedback/mine',{'size':50})['data'])
            row=post('/customer-service/feedback/update',{'id':row['id'],'revision':row['revision'],
                'status':'in_progress','diagnosis':'interaction','note':'非医疗合成软件验收，人工核实为流程测试，不修改资料。'})['data']
            row=post('/customer-service/feedback/update',{'id':row['id'],'revision':row['revision'],
                'status':'closed','diagnosis':'interaction','resolution':'explained',
                'note':'非医疗合成验收已完成：反馈、原始证据留存和处理状态均正常；仅解释关闭，未宣称修复任何医学问题。'})['data']
            detail=post('/customer-service/feedback/detail',{'id':row['id']})['data']
            checks['explainedClosureAudited']=row['resolution']=='explained' and len(detail['events'])==3 and detail['closureEvidence'] is None
            with db_session() as db:
                current=db.get(MarkdownFile,fid)
                checks['documentAndApprovalUnchanged']=before==(current.review_status,current.is_published,current.content_sha256,current.update_time)
            output={'checks':checks,'feedbackId':row['id'],'requestId':scope['requestId'],'fileId':fid,
                    'scope':'Real HTTP/CSRF/SSE/model; NON-MEDICAL fixture only; explained closure, no medical edits/approval/publication'}
            destination=Path('/app/storage/reports/feedback-live-acceptance.json')
            destination.parent.mkdir(parents=True,exist_ok=True)
            destination.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(output,ensure_ascii=False))
            if not all(checks.values()): raise SystemExit(1)
        finally:
            client.post('/api/auth/logout',json={})


if __name__=='__main__':
    main()
