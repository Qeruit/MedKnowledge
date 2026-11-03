"""Read-only corpus snapshot -> disposable DB -> long PDF retrieval regression.

Run in the environment holding the indexed public manuals:
  python -m evaluation.run_long_manuals [--answers] [--grounding] [--strict]
No document is published. The raw-index diagnostic disables page QA only in the
disposable copy, never generates answers, and is not a release acceptance test.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import time
from uuid import uuid4

from fastapi import HTTPException
from pypdf import PdfReader
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from app.config import PROJECT_ROOT, settings as source_settings
from app.db import MarkdownFile, PdfPageAudit, User
from evaluation.public_manual_review_cases import CASES
from evaluation.run_quality import configure


def normalize(value):
    """Ignore PDF layout whitespace, but preserve numbers, units and words."""
    return re.sub(r"\s+", "", value).casefold()


def evidence_match(document, case):
    return (case["page"] is not None
            and str(document.get("metadata", {}).get("pdfPage")) == str(case["page"])
            and all(normalize(term) in normalize(document["content"])
                    for term in case["evidenceTerms"]))


def score(case, trace):
    selected = trace["selected"]
    if case["page"] is None:
        return {"passed": not selected, "emptyExpected": True}
    rankings = [block["rows"] for block in trace["candidates"]]
    return {"passed": any(evidence_match(doc, case) for doc in selected),
            "pageRecallAt": {str(k): int(any(evidence_match(doc, case)
                for rows in rankings for doc in rows[:k])) for k in (3, 5, 16)},
            "selectedPages": [doc.get("metadata", {}).get("pdfPage") for doc in selected]}


def snapshot(engine):
    """Copy only the three public files, their vectors and page audit records."""
    manifest = {m["file"]: m for m in json.loads(
        (PROJECT_ROOT / "data/public_manuals/device_manuals.json").read_text(encoding="utf-8"))}
    files = []
    with engine.connect() as conn:
        conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for filename, cases in CASES.items():
            row = conn.execute(select(MarkdownFile.__table__).where(
                MarkdownFile.original_file_name == filename, MarkdownFile.status == 2)
                .order_by(MarkdownFile.id.desc()).limit(1)).mappings().first()
            if row is None:
                raise RuntimeError(f"No indexed source file: {filename}")
            file = dict(row)
            if file["embedding_model"] and file["embedding_model"] != source_settings.embedding_model:
                raise RuntimeError(f"Indexed/query embedding models differ: {filename}")
            original = Path(file["file_path"])
            sha = hashlib.sha256(original.read_bytes()).hexdigest()
            if sha != manifest[filename]["sha256"] or (file["content_sha256"] and file["content_sha256"] != sha):
                raise RuntimeError(f"Source SHA-256 mismatch: {filename}")
            reader = PdfReader(original)
            if len(reader.pages) != manifest[filename]["pdfPages"]:
                raise RuntimeError(f"Source page count mismatch: {filename}")
            for case in cases:
                if case["page"] and normalize(case["source"]) not in normalize(
                        reader.pages[case["page"] - 1].extract_text() or ""):
                    raise RuntimeError(f"Gold source text missing: {filename}, page {case['page']}")
            vectors = [dict(v) for v in conn.execute(text("""
                SELECT id,content,metadata,embedding::text AS embedding
                FROM t_vector_store WHERE metadata->>'mdStorageId'=:fid ORDER BY id
            """), {"fid": str(file["id"])}).mappings()]
            if not vectors:
                raise RuntimeError(f"No indexed chunks: {filename}")
            audits = [dict(a) for a in conn.execute(select(PdfPageAudit.__table__).where(
                PdfPageAudit.file_id == file["id"])).mappings()]
            files.append({"file": file, "vectors": vectors, "audits": audits,
                          "sha256": sha, "pages": len(reader.pages)})
    return files


def fingerprint(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, default=str).encode()).hexdigest()


def seed(engine, files):
    with engine.begin() as conn:
        conn.execute(User.__table__.insert(), {"id": 1, "username": "offline-evaluator",
            "password_hash": "disabled-offline-fixture", "role": "admin", "is_active": False,
            "create_time": datetime.now()})
        for item in files:
            file = {**item["file"], "owner_id": 1, "reviewer_id": None,
                    "is_published": False}
            conn.execute(MarkdownFile.__table__.insert(), file)
            if item["audits"]:
                conn.execute(PdfPageAudit.__table__.insert(),
                             [{**a, "reviewer_id": None} for a in item["audits"]])
            conn.execute(text("""INSERT INTO t_vector_store(id,content,metadata,embedding)
                VALUES (:id,:content,CAST(:metadata AS json),CAST(:embedding AS vector))"""),
                [{**v, "metadata": json.dumps(v["metadata"])} for v in item["vectors"]])


async def evaluate(files, mode, *, answers=False):
    from app import knowledge
    from app.answer_prompt import answer_messages, evidence_reply, NO_EVIDENCE_REPLY
    from app.llm import chat_chunks
    results = []
    for item in files:
        file = item["file"]
        filename, fid = file["original_file_name"], file["id"]
        audits = {a["page_number"]: a for a in item["audits"]}
        for index, case in enumerate(CASES[filename], 1):
            trace = {}
            start = time.perf_counter()
            documents = await knowledge.find_context(case["question"], user_id=1, role="admin",
                evaluation_file_id=fid, trace=trace)
            row = {"id": f"{filename}:{index}", "mode": mode, "file": filename,
                "question": case["question"], "expectedPage": case["page"],
                "evidenceTerms": case["evidenceTerms"],
                "pageQaStatus": audits.get(case["page"], {}).get("status"),
                "indexedGoldFound": any(evidence_match(v, case) for v in item["vectors"])
                    if case["page"] else None,
                "retrievalMs": round((time.perf_counter() - start) * 1000, 2),
                "retrieval": score(case, trace), "trace": trace}
            if answers:
                answer_start = time.perf_counter()
                error = None
                fallback = evidence_reply(case["question"], documents)
                try:
                    if not fallback:
                        chunks = [chunk async for chunk in chat_chunks(answer_messages(case["question"], documents),
                                  source_settings.customer_model, 0)]
                        answer = "".join(chunks)
                    else:
                        answer = fallback
                except Exception as exc:
                    error = type(exc).__name__
                    answer = ""
                missing = [term for term in case["answerTerms"] if normalize(term) not in normalize(answer)]
                row["answer"] = {"text": answer, "missingTerms": missing, "error": error,
                    "passed": (row["retrieval"]["passed"] and not missing and bool(answer))
                        if case["page"] else (not documents and answer == NO_EVIDENCE_REPLY),
                    "modelCalled": not bool(fallback),
                    "elapsedMs": round((time.perf_counter() - answer_start) * 1000, 2),
                    "check": "evidence page plus keyword smoke check; not semantic or clinical validation"}
            results.append(row)
            print(f"{mode} {filename}:{index} {'PASS' if row['retrieval']['passed'] else 'FAIL'}", flush=True)
    return results


async def scope_checks(files, mode):
    from app.knowledge import find_context
    checks = []
    for target in files:
        fid = target["file"]["id"]
        try:
            await find_context("device instructions", user_id=1, role="admin", file_id=fid)
            blocked = False
        except HTTPException as exc:
            blocked = exc.status_code == 404
        checks.append({"mode": mode, "kind": "unpublished_public_access",
                       "file": target["file"]["original_file_name"], "passed": blocked})
        for other in files:
            if other is target:
                continue
            question = CASES[other["file"]["original_file_name"]][0]["question"]
            documents = await find_context(question, user_id=1, role="admin", evaluation_file_id=fid)
            checks.append({"mode": mode, "kind": "different_device_no_evidence",
                "targetFile": target["file"]["original_file_name"], "question": question,
                "passed": not documents,
                "evidenceWithinRequestedFile": all(str(d["metadata"].get("mdStorageId")) == str(fid) for d in documents),
                "selectedPages": [d["metadata"].get("pdfPage") for d in documents]})
    return checks


def summarize(rows):
    positive = [r for r in rows if r["expectedPage"]]
    negative = [r for r in rows if not r["expectedPage"]]
    times = sorted(r["retrievalMs"] for r in rows)
    return {"factsPassed": sum(r["retrieval"]["passed"] for r in positive), "facts": len(positive),
        "noEvidencePassed": sum(r["retrieval"]["passed"] for r in negative), "noEvidence": len(negative),
        "candidatePageRecall": {str(k): statistics.mean(r["retrieval"]["pageRecallAt"][str(k)] for r in positive)
                                for k in (3, 5, 16)},
        "retrievalP50Ms": statistics.median(times),
        "retrievalP95Ms": times[math.ceil(len(times) * .95) - 1]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--answers", action="store_true", help="Internal model-stream checks for QA-gated evidence only")
    parser.add_argument("--grounding", action="store_true", help="Controlled generation checks on public source snippets")
    parser.add_argument("--strict", action="store_true", help="Exit nonzero on any retrieval or scope failure")
    args = parser.parse_args()
    source = create_engine(source_settings.database_url)
    files = snapshot(source)
    before = fingerprint(files)
    name = "agent_customer_service_eval_" + uuid4().hex[:12]
    assert re.fullmatch(r"agent_customer_service_eval_[a-f0-9]{12}", name)
    admin = create_engine(make_url(source_settings.database_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    target = None
    created = False
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
        created = True
        target = create_engine(make_url(source_settings.database_url).set(database=name))
        with target.begin() as conn:
            conn.exec_driver_sql((PROJECT_ROOT / "schema.sql").read_text(encoding="utf-8"))
        seed(target, files)
        configure(name, PROJECT_ROOT / "storage/quality-eval" / name)
        gated = asyncio.run(evaluate(files, "qa_gated", answers=args.answers))
        checks = asyncio.run(scope_checks(files, "qa_gated"))
        grounding = []
        if args.grounding:
            from evaluation.answer_grounding import evaluate_grounding
            grounding = asyncio.run(evaluate_grounding(files))
        # Diagnostic copy only: retain the raw indexed text without page QA.
        # Neither this copy nor its results can be approved/published by this runner.
        with target.begin() as conn:
            conn.execute(text("DELETE FROM t_pdf_page_audit"))
            conn.execute(text("UPDATE t_ai_customer_service_md_storage SET pdf_qa_revision=0"))
        raw = asyncio.run(evaluate(files, "raw_index_diagnostic"))
        checks += asyncio.run(scope_checks(files, "raw_index_diagnostic"))
        unchanged = before == fingerprint(snapshot(source))
        if not unchanged:
            raise RuntimeError("Source snapshot changed during evaluation; rerun with a stable source")
        from app import answer_prompt, knowledge, pdf_rerank
        report = {"runAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "isolation": "Disposable DB; copied public files/vectors/audits only; source connection READ ONLY; no publication",
            "sourceSnapshotUnchanged": unchanged, "embeddingModel": source_settings.embedding_model,
            "answerModel": source_settings.customer_model if args.answers or args.grounding else None,
            "pipelineConfig": {"retrievalCodeSha256": hashlib.sha256(Path(knowledge.__file__).read_bytes()).hexdigest(),
                "pdfRerankCodeSha256": hashlib.sha256(Path(pdf_rerank.__file__).read_bytes()).hexdigest(),
                "answerPromptCodeSha256": hashlib.sha256(Path(answer_prompt.__file__).read_bytes()).hexdigest(),
                "vectorCandidates": knowledge.CONTEXT_CANDIDATES, "lexicalCandidates": knowledge.LEXICAL_CANDIDATES,
                "contextLimit": knowledge.CONTEXT_LIMIT, "maxContextDistance": knowledge.MAX_CONTEXT_DISTANCE},
            "files": [{"name": i["file"]["original_file_name"], "sha256": i["sha256"],
                       "pages": i["pages"], "chunks": len(i["vectors"]),
                       "indexConfig": i["file"]["index_config"],
                       "pageQaCounts": dict(Counter(a["status"] for a in i["audits"]))} for i in files],
            "summary": {"qaGated": summarize(gated), "rawIndexDiagnostic": summarize(raw)},
            "scopeSummary": {kind: {"passed": sum(c["passed"] for c in checks if c["kind"] == kind),
                                    "total": sum(c["kind"] == kind for c in checks)}
                             for kind in ("unpublished_public_access", "different_device_no_evidence")},
            "scopeChecks": checks, "cases": gated + raw, "groundingCases": grounding,
            "limits": "18 source-checked cases over few pages; exact gold-page coverage can undercount valid duplicate evidence on other pages; diagnostic bypasses page QA in temporary DB only; no release approval; keyword checks cannot prove answer correctness; latencies include real query embedding, sequential warm-cache run"}
        output = PROJECT_ROOT / "reports/quality"
        output.mkdir(parents=True, exist_ok=True)
        path = output / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-long-manuals")
        path.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        lines = ["# 长手册隔离检索回归", "", f"运行时间：{report['runAt']}",
            "三份公开手册的索引和页审计复制到临时数据库；源库只读，资料未发布，结束后删除临时库。", "",
            "| 模式 | 事实证据命中 | 越界无证据 | 候选页 Recall@5 | 检索 P50/P95 (ms) |",
            "|---|---:|---:|---:|---:|"]
        for key, label in (("qaGated", "保留页审核规则"), ("rawIndexDiagnostic", "原始索引诊断（仅临时库）")):
            s = report["summary"][key]
            lines.append(f"| {label} | {s['factsPassed']}/{s['facts']} | {s['noEvidencePassed']}/{s['noEvidence']} | "
                         f"{s['candidatePageRecall']['5']:.3f} | {s['retrievalP50Ms']:.1f}/{s['retrievalP95Ms']:.1f} |")
        access = report["scopeSummary"]["unpublished_public_access"]
        device = report["scopeSummary"]["different_device_no_evidence"]
        lines += ["", f"未发布资料的正常检索入口拦截：{access['passed']}/{access['total']}；"
                  f"跨器械问题无证据检查：{device['passed']}/{device['total']}。",
            "跨器械问题失败指当前文件内选中了不支持问题的片段，不等同于数据库越权或跨文件返回。",
            f"源资料快照未变化：{unchanged}。",
            "", "## 逐题结果", "", "| 手册与题号 | 目标页 | 页审核状态 | 正常命中 | 原始索引命中 | 诊断 |",
            "|---|---:|---|---|---|---|"]
        for g, r in zip(gated, raw, strict=True):
            causes = []
            if not g["retrieval"]["passed"] and g["pageQaStatus"] in {"needs_review", "excluded"}:
                causes.append("目标页受审核规则限制")
            if not r["retrieval"]["passed"]:
                if not r["expectedPage"]:
                    causes.append("越界问题仍选入片段")
                elif not r["indexedGoldFound"]:
                    causes.append("索引缺少完整标注证据")
                elif not r["retrieval"]["pageRecallAt"]["16"]:
                    causes.append("候选召回漏证据")
                else:
                    causes.append("最终选证未覆盖")
            diagnosis = "；".join(causes) or "通过"
            lines.append(f"| {g['id']} | {g['expectedPage'] or '无'} | {g['pageQaStatus'] or '-'} | "
                         f"{g['retrieval']['passed']} | {r['retrieval']['passed']} | {diagnosis} |")
        answer_rows = [r for r in gated if "answer" in r]
        if answer_rows:
            lines += ["", f"正常规则下内部模型流式回答自动检查：{sum(r['answer']['passed'] for r in answer_rows)}/{len(answer_rows)}。",
                      "此项使用生产提示词和模型流，未通过公共 SSE/浏览器接口；完整回答见 JSON，关键词不代表语义正确。"]
        if grounding:
            lines += ["", f"受控证据回答检查：{sum(r['passed'] for r in grounding)}/{len(grounding)}。",
                      "使用指定的公开原文片段测试资料不足、研究期限与设备期限区分、图标缺失和直接证据回答；不是检索或发布验收。"]
        failed_scope = [c for c in checks if not c["passed"]]
        if failed_scope:
            lines += ["", "## 未通过的范围检查", ""] + [f"- {c}" for c in failed_scope]
        lines += ["", "按指定物理页计分：其他页面可能包含同样有效的证据，因此目标页漏命中不等于回答必然错误。",
                  "仅覆盖已标注的少量页面，不代表三份手册的总体准确率。诊断数据不可用于发布验收；当前结果不改变人工审核状态。"]
        path.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"REPORT={path.with_suffix('.md')}", flush=True)
        if args.strict and (any(not r["retrieval"]["passed"] or ("answer" in r and not r["answer"]["passed"])
                                for r in gated + raw) or any(not c["passed"] for c in checks)
                                or any(not c["passed"] for c in grounding)):
            raise SystemExit(1)
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


if __name__ == "__main__":
    main()
