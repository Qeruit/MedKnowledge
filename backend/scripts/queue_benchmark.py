"""Isolated queue experiment; python -m scripts.queue_benchmark --real.

No production files or jobs are consumed. Creates and drops one named temporary
database. Throughput uses controlled embeddings, real pgvector and worker processes.
"""
import argparse
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import logging
import math
import multiprocessing as mp
import os
from pathlib import Path
import platform
import re
import shutil
import time
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import PROJECT_ROOT, settings as original_settings


def percentile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lo, hi = math.floor(position), math.ceil(position)
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def configure(name, overrides=None):
    assert re.fullmatch(r"agent_customer_service_bench_[a-f0-9]{12}", name)
    chosen = replace(original_settings, database_name=name, **(overrides or {}))
    from app import db, documents, knowledge, llm, task_queue, worker
    if db.session_factory.cache_info().currsize:
        db.session_factory().kw["bind"].dispose()
        db.session_factory.cache_clear()
    for module in (db, documents, knowledge, llm, task_queue, worker):
        module.settings = chosen
    return chosen


def event(scenario, kind, file_id=None, worker_id=None, attempt=None, detail=None):
    from app.db import db_session
    with db_session() as db:
        db.execute(text("""INSERT INTO bench_event(scenario,kind,file_id,worker_id,attempt,detail)
            VALUES(:scenario,:kind,:file,:worker,:attempt,CAST(:detail AS json))"""),
            {"scenario":scenario,"kind":kind,"file":file_id,"worker":worker_id,
             "attempt":attempt,"detail":json.dumps(detail or {})})


def events(scenario):
    from app.db import db_session
    with db_session() as db:
        return [dict(row) for row in db.execute(text("SELECT * FROM bench_event WHERE scenario=:s ORDER BY id"),
                                               {"s":scenario}).mappings()]


def run_child(name, scenario, worker_id, gate, file_ids, mode="normal", real=False, overrides=None, stop_after_one=False):
    chosen = configure(name,overrides)
    logging.basicConfig(level=logging.CRITICAL)
    from app import knowledge, llm
    from app.db import db_session
    from app.task_queue import LeaseLost, claim_job, recover_expired
    from app.worker import process_job
    actual_embed = llm.embed_texts_sync
    active = {}
    calls = 0

    def embedding(texts):
        nonlocal calls
        calls += 1
        event(scenario,"embedding_start",active["file_id"],worker_id,active["attempts"],
              {"documents":len(texts),"real":real,"call":calls,"chars":sum(map(len,texts))})
        if real:
            vectors = actual_embed(texts)
        else:
            time.sleep(0.15)
            vectors = [[1.,int(hashlib.sha256(value.encode()).hexdigest()[:6],16)/16777216]
                       + [0.] * (chosen.embedding_dimensions-2) for value in texts]
        event(scenario,"embedding_returned",active["file_id"],worker_id,active["attempts"],
              {"documents":len(texts),"real":real,"call":calls})
        if mode == "crash" and calls == 1:
            event(scenario,"crash",active["file_id"],worker_id,active["attempts"])
            os._exit(75)  # Only this benchmark-owned child, after one paid/simulated response.
        if mode == "retry" and active["attempts"] == 1 and calls == 1:
            raise httpx.ReadError("benchmark: response consumed but result lost before commit")
        if mode == "stale" and calls == 1:
            # Intentionally no heartbeat in this fault-injected child.
            time.sleep(chosen.task_lease_seconds + chosen.task_retry_seconds + 5)
        return vectors

    knowledge.embed_texts_sync = embedding
    event(scenario,"ready",worker_id=worker_id)
    if gate is not None:
        gate.wait(60)
    while True:
        recover_expired()
        job = claim_job(worker_id)
        if job:
            active.clear(); active.update(job)
            event(scenario,"claimed",job["file_id"],worker_id,job["attempts"], {"job":str(job["id"])})
            if mode == "stale":
                try:
                    knowledge.index_markdown(job["file_id"],job)
                    event(scenario,"stale_accepted",job["file_id"],worker_id,job["attempts"])
                except LeaseLost:
                    event(scenario,"stale_blocked",job["file_id"],worker_id,job["attempts"])
            else:
                process_job(job,worker_id)
            with db_session() as db:
                status = db.scalar(text("SELECT status FROM t_index_job WHERE id=:id"), {"id":job["id"]})
            event(scenario,"attempt_finished",job["file_id"],worker_id,job["attempts"], {"status":status})
            if stop_after_one:
                return
        with db_session() as db:
            left = db.scalar(text("""SELECT count(*) FROM t_index_job WHERE file_id=ANY(:ids)
                AND status IN ('queued','running')"""), {"ids":file_ids})
        if not left:
            return
        if not job:
            time.sleep(chosen.task_poll_seconds)


def make_document(path, kib, marker):
    # Common structure and bounded sections; actual parsed chunks are measured.
    section = ("\n## Test section {i}\n" + marker + "\n" +
               "This is synthetic queue benchmark material, not medical guidance. " * 24 + "\n---\n")
    value = "# Queue benchmark\n"
    i = 0
    while len(value.encode()) < kib * 1024:
        value += section.format(i=i)
        i += 1
    path.write_text(value,encoding="utf-8")


def add_file(root, label, kib, scenario):
    from app.db import MarkdownFile,db_session
    from app.documents import read_documents
    from app.task_queue import enqueue
    path = root / (label + ".md")
    make_document(path,kib,label)
    chunks = read_documents(path,path.name)
    with db_session() as db:
        file = MarkdownFile(original_file_name=path.name,new_file_name=path.name,file_path=str(path),
            file_size=path.stat().st_size,status=0,create_time=datetime.now(),update_time=datetime.now())
        db.add(file); db.flush()
        job_id = enqueue(db,file.id)
        info = {"id":file.id,"jobId":str(job_id),"bytes":path.stat().st_size,"chunks":len(chunks),
                "expectedCalls":math.ceil(len(chunks)/10),"path":str(path)}
    event(scenario,"enqueued",info["id"])
    return info


def launch(ctx, name, scenario, worker_id, ids, **options):
    process = ctx.Process(target=run_child,args=(name,scenario,worker_id,options.pop("gate",None),ids),kwargs=options)
    process.start()
    return process


def await_event(scenario,kind,count=1,timeout=60):
    started = time.monotonic()
    while time.monotonic()-started < timeout:
        found = [row for row in events(scenario) if row["kind"] == kind]
        if len(found) >= count:
            return found
        time.sleep(0.1)
    raise TimeoutError(f"Missing {scenario}/{kind} ({count})")


def join_all(processes,timeout=240,allow_crash=False):
    end = time.monotonic()+timeout
    try:
        for process in processes:
            process.join(max(0,end-time.monotonic()))
            if process.is_alive():
                raise TimeoutError("Benchmark-owned child exceeded its time limit")
            if process.exitcode not in ({0,75} if allow_crash else {0}):
                raise RuntimeError(f"Benchmark child exited {process.exitcode}")
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate(); process.join(10)


def validate(file_id):
    from app.db import db_session
    from app.documents import read_documents
    with db_session() as db:
        row = db.execute(text("SELECT * FROM t_ai_customer_service_md_storage WHERE id=:id"), {"id":file_id}).mappings().one()
        expected = read_documents(Path(row["file_path"]),row["original_file_name"])
        stored = db.execute(text("SELECT id,content,metadata FROM t_vector_store WHERE metadata->>'mdStorageId'=:id"),
                            {"id":str(file_id)}).all()
    indices = [entry.metadata["chunkIndex"] for entry in stored]
    valid = (row["status"] == 2 and len(stored) == len(expected) and len(indices) == len(set(indices))
             and all(entry.content == expected[entry.metadata["chunkIndex"]].text for entry in stored)
             and all(entry.metadata["mdStorageId"] == file_id for entry in stored))
    if not valid:
        raise AssertionError(f"Vector consistency failed for benchmark file #{file_id}")
    return {"vectors":len(stored),"duplicateChunks":len(indices)-len(set(indices)),"contentMismatch":0}


def throughput(ctx,name,root,repetitions,jobs):
    output = []
    for kib in (4,32,128):
        for workers in (1,2,4):
            waits,services,end_to_end,elapsed,bytes_seen,chunks_seen,wall_elapsed = [],[],[],[],[],[],[]
            for rep in range(repetitions):
                scenario = f"throughput-{kib}-{workers}-{rep}"
                infos = [add_file(root,f"{scenario}-{i}",kib,scenario) for i in range(jobs)]
                ids = [info["id"] for info in infos]
                gate = ctx.Event()
                children = [launch(ctx,name,scenario,f"{scenario}-worker-{i}",ids,gate=gate) for i in range(workers)]
                try:
                    await_event(scenario,"ready",count=workers)
                    # Use a shared queue time immediately before releasing warmed workers.
                    from app.db import db_session
                    with db_session() as db:
                        db.execute(text("UPDATE t_index_job SET create_time=clock_timestamp(),available_at=clock_timestamp() WHERE file_id=ANY(:ids)"), {"ids":ids})
                    start = time.monotonic(); gate.set()
                    join_all(children)
                    process_wall = time.monotonic()-start
                    wall_elapsed.append(process_wall)
                finally:
                    gate.set()
                    for child in children:
                        if child.is_alive(): child.terminate(); child.join(10)
                with db_session() as db:
                    timings = db.execute(text("""SELECT EXTRACT(EPOCH FROM (c.ts-j.create_time)) AS wait,
                        EXTRACT(EPOCH FROM (j.update_time-c.ts)) AS service,
                        EXTRACT(EPOCH FROM (j.update_time-j.create_time)) AS total
                        FROM t_index_job j JOIN bench_event c ON c.file_id=j.file_id
                        AND c.kind='claimed' AND c.scenario=:s WHERE j.file_id=ANY(:ids)"""), {"s":scenario,"ids":ids}).all()
                waits.extend(float(row.wait) for row in timings)
                services.extend(float(row.service) for row in timings)
                end_to_end.extend(float(row.total) for row in timings)
                # A batch is complete at its last DB completion, not when idle
                # workers finish their final polling sleep and exit.
                elapsed.append(max(float(row.total) for row in timings))
                for info in infos:
                    validate(info["id"])
                    bytes_seen.append(info["bytes"]); chunks_seen.append(info["chunks"])
                print(f"Completed throughput {kib}KiB / {workers} workers / round {rep+1}",flush=True)
            output.append({"targetKiB":kib,"workers":workers,"samples":len(waits),"repetitions":repetitions,
                "actualBytes":bytes_seen[0],"chunksPerFile":chunks_seen[0],
                "filesPerMinute":len(waits)/sum(elapsed)*60,"batchSeconds":elapsed,
                "processWallSeconds":wall_elapsed,
                "queueP50":percentile(waits,.5),"queueP95":percentile(waits,.95),
                "serviceP50":percentile(services,.5),"serviceP95":percentile(services,.95),
                "totalP95":percentile(end_to_end,.95)})
    return output


def crash_recovery(ctx,name,root,repetitions=3,real=False,overrides=None):
    values=[]
    for rep in range(repetitions):
        scenario = f"{'real' if real else 'controlled'}-crash-{rep}"
        info = add_file(root,scenario,4,scenario)
        doomed = launch(ctx,name,scenario,scenario+"-doomed",[info["id"]],mode="crash",real=real,overrides=overrides)
        join_all([doomed],allow_crash=True)
        rescuer = launch(ctx,name,scenario,scenario+"-rescuer",[info["id"]],real=real,overrides=overrides)
        join_all([rescuer],timeout=original_settings.task_lease_seconds+120)
        rows = events(scenario)
        crash = next(e for e in rows if e["kind"] == "crash")["ts"]
        claim = next(e for e in rows if e["kind"] == "claimed" and e["attempt"] == 2)["ts"]
        finished = next(e for e in rows if e["kind"] == "attempt_finished" and e["attempt"] == 2)["ts"]
        cost = call_cost(scenario,info)
        values.append({"reclaimSeconds":(claim-crash).total_seconds(),"completeSeconds":(finished-crash).total_seconds(),
                       "cost":cost,"consistency":validate(info["id"])})
        print(f"Crash recovery round {rep+1}: reclaimed {(claim-crash).total_seconds():.2f}s, completed {(finished-crash).total_seconds():.2f}s",flush=True)
    return values


def call_cost(scenario,info):
    rows = events(scenario)
    calls = [e for e in rows if e["kind"] == "embedding_start"]
    returned = [e for e in rows if e["kind"] == "embedding_returned"]
    return {"baselineCalls":info["expectedCalls"],"calls":len(calls),"returnedCalls":len(returned),
            "extraCalls":len(calls)-info["expectedCalls"],"submittedChunks":sum(e["detail"]["documents"] for e in calls)}


def consistency(ctx,name,root,real=False):
    prefix = "real" if real else "controlled"
    fast = {"task_lease_seconds":6,"task_heartbeat_seconds":1,"task_retry_seconds":1,"task_poll_seconds":.2}
    output={}
    for mode in ("duplicate","retry"):
        scenario=prefix+"-"+mode
        info=add_file(root,scenario,4,scenario)
        if mode == "duplicate":
            from concurrent.futures import ThreadPoolExecutor
            from app.db import db_session
            from app.task_queue import enqueue
            def duplicate(_):
                with db_session() as db:
                    return str(enqueue(db,info["id"]))
            with ThreadPoolExecutor(max_workers=8) as pool:
                job_ids=list(pool.map(duplicate,range(8)))
            assert set(job_ids) == {info["jobId"]}
        child=launch(ctx,name,scenario,scenario+"-worker",[info["id"]],
                     mode="normal" if mode == "duplicate" else mode,real=real,overrides=fast)
        join_all([child])
        output[mode]={"cost":call_cost(scenario,info),"consistency":validate(info["id"])}
    if not real:
        scenario=prefix+"-stale"
        info=add_file(root,scenario,4,scenario)
        old=launch(ctx,name,scenario,scenario+"-old",[info["id"]],mode="stale",overrides=fast,stop_after_one=True)
        await_event(scenario,"embedding_returned")
        path=Path(info["path"])
        path.write_text(path.read_text(encoding="utf-8").replace(scenario,"NEW-VERSION-MARKER"),encoding="utf-8")
        new=launch(ctx,name,scenario,scenario+"-new",[info["id"]],overrides=fast)
        join_all([old,new])
        rows=events(scenario)
        assert any(e["kind"] == "stale_blocked" for e in rows)
        assert not any(e["kind"] == "stale_accepted" for e in rows)
        output["stale"]={"cost":call_cost(scenario,info),"consistency":validate(info["id"]),"oldWriteBlocked":True}
    return output


def write_report(result,path):
    lines=["# 持久化任务队列量化实验", "", f"运行标识：`{result['runId']}`", "",
        "## 方法与边界", "",
        "独立临时 PostgreSQL 数据库、真实 Markdown 解析、pgvector 索引与独立 Worker 进程。吞吐部分使用每次固定延迟 150ms 的测试 Embedding，不能当作模型供应商吞吐或生产容量。Worker 进程及数据库连接先启动，首次解析初始化仍计入处理耗时。任务统一起跑；吞吐窗口结束于最后一个任务的数据库完成时间，不计空闲 Worker 退出前的轮询等待，也不含进程启动和资料上传。文件等待时间从统一起跑至领取记录，完成时间来自数据库事务时间戳。P50/P95 使用线性插值，样本量有限。逐事件落库会增加测量开销；测试按固定顺序执行且索引数据逐步累积，不是随机化、固定表容量的生产压测。", "",
        "故障恢复使用默认生产租约、退避和轮询值；记录在异常退出前落库，起点与进程实际退出存在很小间隔。故障注入发生在一次 Embedding 返回后、向量提交前。", "",
        "一致性重试/旧结果测试使用 6s 租约、1s 心跳、1s 退避、0.2s 轮询以缩短实验，不用于宣称默认恢复时延。额外请求数不等于货币成本，未读取供应商账单；真实模式返回结果的调用计入返回请求数。", "",
        f"环境：{result['environment']}", "",f"默认队列配置：{result['queueConfig']}", "",
        "## 吞吐与排队", "",
        "|目标规模|实际字节/片段|Worker|样本|文件/分钟|排队P50(s)|排队P95(s)|处理P95(s)|端到端P95(s)|",
        "|---|---|---|---|---|---|---|---|---|"]
    for row in result["throughput"]:
        lines.append(f"|{row['targetKiB']} KiB|{row['actualBytes']}/{row['chunksPerFile']}|{row['workers']}|{row['samples']}|{row['filesPerMinute']:.2f}|{row['queueP50']:.3f}|{row['queueP95']:.3f}|{row['serviceP95']:.3f}|{row['totalP95']:.3f}|")
    lines += ["", "## 默认配置故障恢复", "", "|轮次|重新领取(s)|恢复完成(s)|额外Embedding请求|重复片段|", "|---|---|---|---|---|"]
    for index,row in enumerate(result["recovery"],1):
        lines.append(f"|{index}|{row['reclaimSeconds']:.3f}|{row['completeSeconds']:.3f}|{row['cost']['extraCalls']}|{row['consistency']['duplicateChunks']}|")
    completions=[row["completeSeconds"] for row in result["recovery"]]
    lines += ["",f"恢复完成耗时：最小 {min(completions):.3f}s，中位数 {percentile(completions,.5):.3f}s，最大 {max(completions):.3f}s。仅 {len(completions)} 次故障，不能据此承诺恢复 SLA。"]
    lines += ["", "## 一致性与调用开销", "", "|模式|场景|基线请求|实际请求|额外请求|重复片段|内容不匹配|", "|---|---|---|---|---|---|---|"]
    for kind in ("controlledConsistency","realConsistency"):
        for scenario,row in result.get(kind,{}).items():
            c,v=row["cost"],row["consistency"]
            lines.append(f"|{'真实模型' if kind.startswith('real') else '受控'}|{scenario}|{c['baselineCalls']}|{c['calls']}|{c['extraCalls']}|{v['duplicateChunks']}|{v['contentMismatch']}|")
    lines += ["", "原始逐事件数据及全部参数见同目录 JSON 文件。仅清理本次创建的数据库和临时文件，现有知识库不变。", ""]
    path.write_text("\n".join(lines),encoding="utf-8")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real",action="store_true",help="Add a few real provider calls for duplicate/retry cost validation")
    parser.add_argument("--jobs",type=int,default=12)
    parser.add_argument("--repetitions",type=int,default=2)
    parser.add_argument("--recovery-repetitions",type=int,default=3)
    args=parser.parse_args()
    if not (2 <= args.jobs <= 100 and 1 <= args.repetitions <= 5 and 1 <= args.recovery_repetitions <= 5):
        parser.error("Samples outside safe benchmark bounds")
    run_id=uuid4().hex[:12]
    name="agent_customer_service_bench_"+run_id
    root=(PROJECT_ROOT/"storage"/"bench"/run_id).resolve()
    root.mkdir(parents=True,exist_ok=False)
    admin_url=make_url(original_settings.source_database_url).set(database="postgres")
    admin_engine=create_engine(admin_url,isolation_level="AUTOCOMMIT")
    created=False
    result={"runId":run_id,"environment":{"python":platform.python_version(),"os":platform.platform(),"cpuCount":os.cpu_count()},
        "queueConfig":{key:getattr(original_settings,key) for key in
                       ("task_lease_seconds","task_heartbeat_seconds","task_retry_seconds","task_poll_seconds","task_max_attempts")}}
    reports=PROJECT_ROOT/"reports"/"queue_benchmark"
    reports.mkdir(parents=True,exist_ok=True)
    stem=datetime.now().strftime("%Y%m%d-%H%M%S")+"-"+run_id
    try:
        with admin_engine.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
        created=True
        chosen=configure(name)
        engine=create_engine(chosen.database_url)
        with engine.begin() as conn:
            conn.exec_driver_sql((PROJECT_ROOT/"schema.sql").read_text(encoding="utf-8"))
            conn.exec_driver_sql("""CREATE TABLE bench_event(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                scenario TEXT NOT NULL,kind TEXT NOT NULL,file_id BIGINT,worker_id TEXT,attempt INTEGER,
                detail JSON,ts TIMESTAMP NOT NULL DEFAULT clock_timestamp())""")
            result["environment"]["postgres"]=conn.scalar(text("SELECT version()"))
        engine.dispose()
        print(f"Isolated benchmark database created: {name}",flush=True)
        ctx=mp.get_context("spawn")
        result["throughput"]=throughput(ctx,name,root,args.repetitions,args.jobs)
        result["recovery"]=crash_recovery(ctx,name,root,args.recovery_repetitions)
        result["controlledConsistency"]=consistency(ctx,name,root)
        if args.real:
            result["realConsistency"]=consistency(ctx,name,root,real=True)
        from app.db import db_session
        with db_session() as db:
            raw=[dict(row) for row in db.execute(text("SELECT * FROM bench_event ORDER BY id")).mappings()]
        result["rawEvents"]=raw
        result["status"]="passed"
        write_report(result,reports/(stem+".md"))
    except Exception as exc:
        result["status"]="failed"
        result["error"]=type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        from app.db import session_factory
        if session_factory.cache_info().currsize:
            session_factory().kw["bind"].dispose(); session_factory.cache_clear()
        if created:
            # Exact UUID-suffixed DB created above, never original or product database.
            assert re.fullmatch(r"agent_customer_service_bench_[a-f0-9]{12}",name)
            with admin_engine.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE "{name}"')
        admin_engine.dispose()
        assert root.parent == (PROJECT_ROOT/"storage"/"bench").resolve()
        shutil.rmtree(root)
        result["cleanup"]="temporary database and files removed"
        (reports/(stem+".json")).write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
        print(f"Benchmark artifacts: {reports / stem}",flush=True)


if __name__ == "__main__":
    main()
