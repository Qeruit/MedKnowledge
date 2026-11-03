"""Compare stored v1/v2 in a disposable database; never approve/publish sources.

QA-gated retrieval remains separate from diagnostic (temporary copy only) answer
tests. The latter removes page QA solely from the disposable copy and uses the
production retrieval/prompt/internal model stream; NOT a public SSE release test.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from app.config import PROJECT_ROOT, settings as source_settings
from app.db import MarkdownFile, PdfPageAudit
from evaluation.accelstim_repair_cases import CASES
from evaluation.run_long_manuals import seed, fingerprint
from evaluation.run_quality import configure

SOURCE_HASH = "c3fa82d25e335dc9beb39f371b9c47984812936029b00dbe5cb7a4c8ab03bb40"


def normalized(value):
    value = "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", value).casefold()


def snapshot(engine, ids):
    files = []
    with engine.connect() as conn:
        conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for fid in ids:
            row = conn.execute(select(MarkdownFile.__table__).where(MarkdownFile.id == fid)).mappings().one()
            file = dict(row)
            if file["status"] != 2 or file["is_published"] or file["content_sha256"] != SOURCE_HASH:
                raise RuntimeError("Only the known indexed, unpublished source versions may be compared")
            if hashlib.sha256(Path(file["file_path"]).read_bytes()).hexdigest() != SOURCE_HASH:
                raise RuntimeError("Source PDF hash mismatch")
            if file["embedding_model"] != source_settings.embedding_model:
                raise RuntimeError("Embedding model differs from stored index")
            vectors = [dict(v) for v in conn.execute(text("SELECT id,content,metadata,embedding::text AS embedding FROM t_vector_store WHERE metadata->>'mdStorageId'=:fid ORDER BY id"), {"fid":str(fid)}).mappings()]
            audits = [dict(a) for a in conn.execute(select(PdfPageAudit.__table__).where(PdfPageAudit.file_id == fid)).mappings()]
            if not vectors or len(audits) != 72:
                raise RuntimeError("Expected complete source/audit snapshot")
            files.append({"file":file,"vectors":vectors,"audits":audits,"sha256":SOURCE_HASH,"pages":72})
    return files


def evidence_check(case, documents):
    if not case["pages"]:
        return {"passed":not documents,"matchedPages":[]}
    relevant = [d for d in documents if int(d["metadata"].get("pdfPage",0)) in case["pages"]]
    body = normalized("\n".join(d["content"] for d in relevant))
    hits = [bool(re.search(pattern,body,re.I)) for pattern in case["patterns"]]
    return {"passed":bool(relevant) and all(hits),"patternHits":hits,
            "matchedPages":sorted({int(d["metadata"]["pdfPage"]) for d in relevant})}


def json_object(answer):
    for match in re.finditer(r"\{", answer):
        try:
            value, _ = json.JSONDecoder().raw_decode(answer[match.start():])
            if isinstance(value,dict):
                return value
        except ValueError:
            pass
    return None


def matches(expected, actual):
    if isinstance(expected,dict):
        return isinstance(actual,dict) and all(k in actual and matches(v,actual[k]) for k,v in expected.items())
    if isinstance(expected,list):
        return isinstance(actual,list) and len(expected)==len(actual) and all(matches(e,a) for e,a in zip(expected,actual))
    try:
        return not isinstance(actual,bool) and float(str(actual).rstrip("%"))==float(expected)
    except (ValueError,TypeError):
        return actual==expected


def answer_check(case, answer, documents):
    from app.answer_prompt import NO_EVIDENCE_REPLY, SOURCE_CONFLICT_REPLY
    if "expected" in case:
        parsed = json_object(answer)
        return {"passed":matches(case["expected"],parsed),"parsed":parsed,
                "formatFailure":parsed is None,"check":"category-bound JSON field equality"}
    kind = case["kind"]
    if kind=="refusal":
        passed = not documents and answer==NO_EVIDENCE_REPLY
    elif kind=="conflict":
        placement = re.search(r"(?:应|至少|保持|keep|maintain|minimum).{0,30}\d(?:[.,]\d+)?\s*(?:米|m\b|met(?:er|re))",answer,re.I)
        passed = answer==SOURCE_CONFLICT_REPLY or bool(not placement and re.search(r"差异|不一致|inconsisten|discrepan", answer,re.I)
            and re.search(r"不能|无法|确认|核对|consult|confirm|cannot|not.*(?:calculate|placement)", answer,re.I))
    elif kind=="expired":
        clauses = re.split(r"[。；;\n]", answer)
        risky = [c for c in clauses if re.search(r"凝胶|gel|重启|restart",c,re.I)
                 and not re.search(r"无需|不要|不应|不需要|不能|不适用|not|don't|no need",c,re.I)]
        return {"passed":bool(re.search(r"到期|届满|expired|caducad",answer,re.I)) and "365" in answer and not risky,
                "riskyActionClauses":risky,"check":"bounded state/action smoke check; AI review required"}
    else:
        passed = bool(re.search(r"凝胶|gel", answer,re.I) and re.search(r"重启|restart|关机.*开机|turn.*off", answer,re.I)
                      and re.search(r"联系|客服|patient care|contact",answer,re.I))
    return {"passed":bool(answer) and passed,"check":"bounded smoke check; AI review required"}


async def run_mode(files, mode, *, answers=False):
    from app.knowledge import find_context
    from app.answer_prompt import answer_messages, evidence_reply
    from app.llm import chat_chunks
    from app.main import source_cards
    rows = []
    for item in files:
        fid = item["file"]["id"]
        for case in CASES:
            trace = {}
            started = time.perf_counter()
            row = {"fileId":fid,"version":item["file"]["version_number"],"mode":mode,
                   "caseId":case["id"],"group":case["group"],"question":case["question"],
                   "expectedPages":case["pages"],"expected":case.get("expected"),"error":None}
            try:
                documents = await asyncio.wait_for(find_context(case["question"],user_id=1,role="admin",evaluation_file_id=fid,trace=trace),60)
                row.update({"retrievalMs":round((time.perf_counter()-started)*1000,2),"evidence":evidence_check(case,documents),
                            "selected":documents,"sources":source_cards(documents),"trace":trace})
                if answers:
                    fallback = evidence_reply(case["question"],documents)
                    async def generate():
                        return "".join([c async for c in chat_chunks(answer_messages(case["question"],documents),source_settings.customer_model,0)])
                    start_answer = time.perf_counter()
                    answer = fallback if fallback else await asyncio.wait_for(generate(),120)
                    row.update({"answer":answer,"modelCalled":not bool(fallback),
                                "answerMs":round((time.perf_counter()-start_answer)*1000,2),
                                "answerCheck":answer_check(case,answer,documents)})
                    row["passed"] = row["evidence"]["passed"] and row["answerCheck"]["passed"]
                else:
                    row["passed"] = row["evidence"]["passed"]
            except Exception as exc:
                row.update({"error":type(exc).__name__,"passed":False,"trace":trace})
            rows.append(row)
            print(f"{mode} v{row['version']} {case['id']} {'PASS' if row['passed'] else 'FAIL'}",flush=True)
    return rows


async def public_checks(files, mode):
    from app.knowledge import find_context
    checks = []
    for item in files:
        fid=item["file"]["id"]
        try:
            await find_context("AccelStim instructions",user_id=1,role="admin",file_id=fid)
            blocked=False
        except HTTPException as exc:
            blocked=exc.status_code==404
        checks.append({"mode":mode,"fileId":fid,"passed":blocked})
    return checks


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-id",type=int,default=2)
    parser.add_argument("--new-id",type=int,default=7)
    parser.add_argument("--strict",action="store_true")
    args=parser.parse_args()
    if args.old_id==args.new_id:
        parser.error("Distinct versions required")
    source=create_engine(source_settings.database_url)
    files=snapshot(source,[args.old_id,args.new_id])
    if files[0]["file"]["document_key"]!=files[1]["file"]["document_key"]:
        raise RuntimeError("Versions do not belong to the same document")
    before=fingerprint(files)
    name="agent_customer_service_repair_"+uuid4().hex[:12]
    assert re.fullmatch(r"agent_customer_service_repair_[a-f0-9]{12}",name)
    admin=create_engine(make_url(source_settings.database_url).set(database="postgres"),isolation_level="AUTOCOMMIT")
    target=None
    created=False
    report=None
    output=PROJECT_ROOT/"reports/demo/accelstim_repair"
    output.mkdir(parents=True,exist_ok=True)
    path=output/(datetime.now().strftime("%Y%m%d-%H%M%S")+"-regression")
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
        created=True
        target=create_engine(make_url(source_settings.database_url).set(database=name))
        with target.begin() as conn:
            conn.exec_driver_sql((PROJECT_ROOT/"schema.sql").read_text(encoding="utf-8"))
        seed(target,files)
        chosen=configure(name,PROJECT_ROOT/"storage/quality-eval"/name)
        # Fail closed before any retrieval, including on dotenv/config drift.
        from app import db
        assert db.settings.database_name==name
        gated=asyncio.run(run_mode(files,"qa_gated_retrieval"))
        checks=asyncio.run(public_checks(files,"qa_gated"))
        with target.begin() as conn:
            conn.execute(text("DELETE FROM t_pdf_page_audit"))
            conn.execute(text("UPDATE t_ai_customer_service_md_storage SET pdf_qa_revision=0"))
        diagnostic=asyncio.run(run_mode(files,"temporary_diagnostic_answers",answers=True))
        checks+=asyncio.run(public_checks(files,"temporary_diagnostic"))
        unchanged=before==fingerprint(snapshot(source,[args.old_id,args.new_id]))
        if not unchanged:
            raise RuntimeError("Source versions changed; results require rerun")
        summaries={}
        for fid in (args.old_id,args.new_id):
            subset=[r for r in diagnostic if r["fileId"]==fid]
            summaries[str(fid)]={"total":len(subset),"evidencePassed":sum(r.get("evidence",{}).get("passed",False) for r in subset),
                "answerCheckPassed":sum(r.get("answerCheck",{}).get("passed",False) for r in subset),
                "jointPassed":sum(r["passed"] for r in subset),"modelCalls":sum(r.get("modelCalled",False) for r in subset),
                "errors":sum(r["error"] is not None for r in subset)}
        report={"runAt":datetime.now().astimezone().isoformat(),"oldId":args.old_id,"newId":args.new_id,
            "sourceSnapshotUnchanged":unchanged,"sourceSha256":SOURCE_HASH,"model":source_settings.customer_model,
            "embedding":source_settings.embedding_model,"sourceStates":[{"id":f["file"]["id"],"status":f["file"]["review_status"],
                 "published":f["file"]["is_published"],"chunks":len(f["vectors"])} for f in files],
            "codeHashes":{n:hashlib.sha256((PROJECT_ROOT/p).read_bytes()).hexdigest() for n,p in {
                "cases":"evaluation/accelstim_repair_cases.py","retrieval":"app/knowledge.py","rerank":"app/pdf_rerank.py",
                "prompt":"app/answer_prompt.py","repair":"app/accelstim_tables.py"}.items()},
            "summary":summaries,"publicEntryChecks":checks,"qaGated":gated,"diagnostic":diagnostic,
            "limits":"12 fixed public-PDF cases. QA disabled only in disposable diagnostic copy; no approval or publication. Internal model stream, NOT public SSE/browser acceptance. JSON checks bind fields to values; remaining bounded checks need AI/manual review. Not general accuracy or clinical validation."}
    finally:
        from app import db
        if db.session_factory.cache_info().currsize:
            db.session_factory().kw["bind"].dispose()
            db.session_factory.cache_clear()
        if target is not None:
            target.dispose()
        if created:
            with admin.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        source.dispose()
        admin.dispose()
        print("Disposable diagnostic database removed.",flush=True)
    if report is None:
        raise RuntimeError("No completed report")
    report["temporaryDatabaseRemoved"]=True
    path.with_suffix(".json").write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    lines=["# 修复前后检索与回答专项回归","",f"时间：{report['runAt']}",
        "源库只读快照；QA规则保留检查与临时副本诊断分开。诊断副本中的回答不是已发布资料的公共SSE或浏览器验收。",
        f"源版本快照未变：{report['sourceSnapshotUnchanged']}；临时库已删除：True。","",
        "| 版本ID | 题数 | 最终证据通过 | 回答检查通过 | 联合通过 | 真实模型调用 | 错误 |","|---|---:|---:|---:|---:|---:|---:|"]
    for fid,s in summaries.items():
        lines.append(f"| {fid} | {s['total']} | {s['evidencePassed']} | {s['answerCheckPassed']} | {s['jointPassed']} | {s['modelCalls']} | {s['errors']} |")
    lines += ["","## 临时诊断副本逐题结果","","| 题号 | 旧证据/回答/联合 | 新证据/回答/联合 | 新版检索页 |","|---|---|---|---|"]
    for case in CASES:
        old=next(r for r in diagnostic if r["fileId"]==args.old_id and r["caseId"]==case["id"])
        new=next(r for r in diagnostic if r["fileId"]==args.new_id and r["caseId"]==case["id"])
        def status(r):
            return f"{r.get('evidence',{}).get('passed',False)}/{r.get('answerCheck',{}).get('passed',False)}/{r['passed']}"
        pages=sorted({d['metadata'].get('pdfPage') for d in new.get('selected',[])})
        lines.append(f"| {case['id']} | {status(old)} | {status(new)} | {pages} |")
    lines += ["",f"未发布资料正常入口拦截：{sum(c['passed'] for c in checks)}/{len(checks)}。",
        "真实问题、候选排名/距离/评分、完整所选片段、来源卡及回答见同名JSON。QA保留模式没有调用生成模型；原始副本诊断只用于定位修复效果。",
        "固定少量样本、结构化输出约束和规则评分不能代表总体正确率。失败题保留，必须进一步核对完整回答；禁止将临时QA诊断通过作为人工审核或发布依据。"]
    path.with_suffix(".md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(f"REPORT={path.with_suffix('.md')}",flush=True)
    if args.strict and (not all(c["passed"] for c in checks) or any(not r["passed"] for r in diagnostic if r["fileId"]==args.new_id)):
        raise SystemExit(1)


if __name__=="__main__":
    main()
