"""Real isolated backup/restore, alerts, permissions, chunk resume and RAG drill."""
import argparse
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,select,text
from sqlalchemy.engine import make_url

from app.config import PROJECT_ROOT,settings as source_settings


def configure(database,storage):
    from app import db,auth,main,operations,ops_agent,llm,knowledge,resumable_upload,worker,task_queue,documents
    if db.session_factory.cache_info().currsize:
        db.session_factory().kw["bind"].dispose(); db.session_factory.cache_clear()
    chosen=replace(source_settings,database_name=database,md_storage_path=Path(storage),
                   task_lease_seconds=6,task_heartbeat_seconds=1,task_retry_seconds=1,
                   task_poll_seconds=.2,alert_hold_seconds=2,monitor_interval_seconds=1)
    for module in (db,auth,main,operations,ops_agent,llm,knowledge,resumable_upload,worker,task_queue,documents):
        module.settings=chosen
    return chosen


def stalled_worker(database,storage,file_id):
    configure(database,storage)
    from app import knowledge
    from app.worker import run_once
    original=knowledge.embed_texts_sync
    def delayed(texts):
        time.sleep(120)  # Parent intentionally terminates ONLY this drill-owned process.
        return original(texts)
    knowledge.embed_texts_sync=delayed
    run_once("operations-drill-worker",file_id=file_id)


def login(username,password):
    from app.main import app
    client=TestClient(app)
    response=client.post("/auth/login",json={"username":username,"password":password})
    assert response.status_code==200
    client.headers["X-CSRF-Token"]=response.json()["data"]["csrfToken"]
    return client


def cleanup(database):
    assert re.fullmatch(r"agent_customer_service_restore_[a-f0-9]{12}",database)
    engine=create_engine(make_url(source_settings.database_url).set(database="postgres"),isolation_level="AUTOCOMMIT")
    with engine.connect() as conn: conn.exec_driver_sql(f'DROP DATABASE "{database}"')
    engine.dispose()
    root=(PROJECT_ROOT/"restores"/database).resolve()
    assert root.parent==(PROJECT_ROOT/"restores").resolve()
    shutil.rmtree(root)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-id")
    parser.add_argument("--worker",nargs=3,metavar=("DATABASE","STORAGE","FILE_ID"))
    args=parser.parse_args()
    if args.worker:
        stalled_worker(args.worker[0],args.worker[1],int(args.worker[2])); return
    from app.backups import create_backup,restore_backup,verify_backup
    from app.db import User,MarkdownFile,FileGrant,db_session,session_factory
    from app.auth import hash_password,COOKIE
    from app.task_queue import enqueue
    from app import llm
    from app.operations import snapshot,evaluate
    from app.worker import run_once
    report={"startedAt":datetime.now().isoformat(),"method":"isolated restored databases; actual pg_dump/pg_restore and real RAG"}
    databases=[]
    child=None
    started=time.perf_counter()
    reports=PROJECT_ROOT/"reports"/"operations"
    reports.mkdir(parents=True,exist_ok=True)
    stem=datetime.now().strftime("%Y%m%d-%H%M%S")+"-"+uuid4().hex[:8]
    try:
        if args.backup_id:
            _,base=verify_backup(args.backup_id)
        else: base=create_backup()
        report["businessBackup"]={key:base[key] for key in ("id","bytes","durationSeconds")}
        first="agent_customer_service_restore_"+uuid4().hex[:12]
        restored=restore_backup(base["id"],first)
        databases.append(first)
        report["businessRestore"]=restored
        chosen=configure(first,restored["storage"])
        users={}
        password="Drill-only-password-2026"
        with db_session() as db:
            for role in ("admin","editor","viewer"):
                user=User(username="dr_"+role+"_"+uuid4().hex[:8],password_hash=hash_password(password),role=role)
                db.add(user); db.flush(); users[role]={"id":user.id,"username":user.username}
            file=db.scalar(select(MarkdownFile).where(MarkdownFile.status==2).order_by(MarkdownFile.id))
            if file is None: raise RuntimeError("演练需要至少一份处理完成的资料")
            file.owner_id=users["editor"]["id"]; file.is_published=True
            primary=file.id
            db.add(FileGrant(file_id=primary,user_id=users["viewer"]["id"]))
            queue_files=[]
            for index in range(2):
                path=chosen.md_storage_path/f"drill-queue-{index}.md"
                path.write_text("# Software test\nRestore queue test only, not medical guidance.",encoding="utf-8")
                item=MarkdownFile(original_file_name=path.name,new_file_name=path.name,file_path=str(path),
                    file_size=path.stat().st_size,status=0,owner_id=users["editor"]["id"],
                    create_time=datetime.now(),update_time=datetime.now())
                db.add(item); db.flush(); queue_files.append(item.id); enqueue(db,item.id)
            db.execute(text("UPDATE t_index_job SET create_time=clock_timestamp()-interval '700 seconds' WHERE file_id=:id"),{"id":queue_files[1]})
            db.execute(text("DELETE FROM t_ops_event"))
        admin=login(users["admin"]["username"],password)
        viewer=login(users["viewer"]["username"],password)
        editor=login(users["editor"]["username"],password)
        first_part=b"a"*(4*1024*1024)
        tail=b"tail-software-test"
        init=editor.post("/customer-service/md/uploads",json={"fileName":"drill-resume.txt",
            "fileSize":len(first_part)+len(tail),"sha256":hashlib.sha256(first_part+tail).hexdigest()}).json()["data"]
        upload_id=init["uploadId"]
        chunk=editor.put(f"/customer-service/md/uploads/{upload_id}/chunks/0",content=first_part,
                         headers={"X-Chunk-Sha256":hashlib.sha256(first_part).hexdigest()})
        assert chunk.status_code==200
        old_cookie=viewer.cookies.get(COOKIE)
        child=subprocess.Popen([sys.executable,"-m","scripts.operations_drill","--worker",first,
                                 str(chosen.md_storage_path),str(queue_files[0])],cwd=PROJECT_ROOT,
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if snapshot()["workersAlive"]>0: break
            time.sleep(.1)
        else: raise TimeoutError("测试 Worker 没有心跳")
        evaluate(snapshot())
        fault_at=time.perf_counter()
        child.terminate(); child.wait(10); child=None
        original=llm._embed_texts_sync
        def failure(_): raise httpx.ReadError("drill injected model failure")
        llm._embed_texts_sync=failure
        try:
            for _ in range(3):
                try: llm.embed_texts_sync(["nonmedical test"])
                except httpx.ReadError: pass
        finally: llm._embed_texts_sync=original
        fired=[]
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            fired += evaluate(snapshot())
            if any(item["rule"]=="worker_missing" and item["state"]=="firing" for item in fired): break
            time.sleep(.2)
        else: raise TimeoutError("未触发测试 Worker 心跳告警")
        report["alertDetectionSeconds"]=time.perf_counter()-fault_at
        report["triggeredRules"]=sorted({item["rule"] for item in fired if item["state"]=="firing"})
        assert {"worker_missing","model_errors","queue_stalled"}.issubset(report["triggeredRules"])
        # Actual fixture backup includes all roles, grants, queued/running jobs and a 4MiB part.
        fixture=create_backup()
        report["fixtureBackup"]={key:fixture[key] for key in ("id","bytes","durationSeconds")}
        second="agent_customer_service_restore_"+uuid4().hex[:12]
        final=restore_backup(fixture["id"],second)
        databases.append(second)
        report["fixtureRestore"]=final
        configure(second,final["storage"])
        from app.main import app
        expired=TestClient(app); expired.cookies.set(COOKIE,old_cookie)
        assert expired.get("/auth/me").status_code==401
        admin=login(users["admin"]["username"],password)
        viewer=login(users["viewer"]["username"],password)
        editor=login(users["editor"]["username"],password)
        assert viewer.get("/ops/status").status_code==403
        assert viewer.post("/customer-service/md/list",json={}).status_code==403
        accessible=viewer.get("/customer-service/md/available").json()["data"]
        assert any(file["id"]==primary for file in accessible)
        assert editor.get(f"/customer-service/md/uploads/{upload_id}").json()["data"]["uploadedChunks"]==[0]
        with db_session() as db:
            db.execute(text("DELETE FROM t_ops_event WHERE outcome='error'"))
        queued_started=time.perf_counter()
        for file_id in queue_files:
            deadline=time.monotonic()+15
            while not run_once("drill-restored-worker",file_id=file_id):
                if time.monotonic()>deadline: raise TimeoutError("恢复任务未能领取")
                time.sleep(.2)
        report["restoredQueueSeconds"]=time.perf_counter()-queued_started
        question="ActaStim-S 的存储温度是多少？"
        answer=viewer.post("/customer-service/completion",json={"message":question,"fileId":primary})
        visible="".join(json.loads(line[6:]).get("v","") for line in answer.text.splitlines() if line.startswith("data: "))
        assert answer.status_code==200 and "event: sources" in answer.text and "event: error" not in answer.text
        assert "-25" in visible and "70" in visible
        resolved=evaluate(snapshot())
        report["finalAlertStates"]={row["rule"]:row["state"] for row in snapshot()["alerts"]}
        # Alerts were intentionally cleared during restore; also prove live firing -> resolved transitions.
        with db_session() as db:
            db.execute(text("""UPDATE t_ops_alert SET state='firing',first_seen=clock_timestamp()
                WHERE rule IN ('worker_missing','queue_stalled','model_errors')"""))
        recovered=evaluate(snapshot())
        report["resolvedRules"]=sorted({item["rule"] for item in recovered if item["state"]=="resolved"})
        assert {"worker_missing","model_errors","queue_stalled"}.issubset(report["resolvedRules"])
        report["checks"]={"coreTableCountsAndHashes":True,"originalFilesAndPartHashes":True,
            "oldSessionRejected":True,"rolesAndGrantEnforced":True,"uploadResume":True,
            "pendingJobsCompleted":True,"realRagAnswerAndSource":True}
        report["status"]="passed"
    finally:
        if child is not None and child.poll() is None: child.terminate(); child.wait(10)
        if session_factory.cache_info().currsize:
            session_factory().kw["bind"].dispose(); session_factory.cache_clear()
        for database in reversed(databases): cleanup(database)
        report["cleanup"]="drill databases and restored volumes removed; backup archives retained privately"
        report["totalSeconds"]=time.perf_counter()-started
        (reports/(stem+".json")).write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
        lines=["# 监控、备份与隔离恢复演练","",f"结果：{report.get('status','failed')}","",
            "业务库只执行备份；故障注入、账号授权及分片测试均在恢复出的临时库中进行。数据库故障回退与 SSE 200 下的错误计数另由自动化测试验证。", "",
            "本次心跳故障演练使用 6 秒租约、2 秒告警保持时间；不是默认生产告警时延。模型错误由可控故障注入产生，最终问答调用真实模型。", "",
            "恢复逐表核对数量和 SHA-256，文件及分片逐个校验；恢复后清空登录会话与心跳，运行恢复的待执行任务，验证资料授权及真实 SSE 问答。", "",
            "```json",json.dumps(report,ensure_ascii=False,indent=2,default=str),"```",""]
        (reports/(stem+".md")).write_text("\n".join(lines),encoding="utf-8")
        print(f"Drill report: {reports/stem}")


if __name__=="__main__": main()
