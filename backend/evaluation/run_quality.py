"""Run the real retrieval stack against a disposable database and fixed evidence labels.

Usage: python -m evaluation.run_quality [--answers] [--strict]
Only the checked-in, partial public demonstration corpus is copied/indexed.
"""
import argparse
import asyncio
from collections import defaultdict
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import statistics
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import PROJECT_ROOT, settings as source_settings
from evaluation.cases import FACTS, cases
from evaluation.quality import answer_check, matches, parse_sse, retrieval_scores


def configure(name, storage):
    from app import auth, db, documents, knowledge, llm, main, observability, operations, resumable_upload, task_queue, worker
    if db.session_factory.cache_info().currsize:
        db.session_factory().kw["bind"].dispose()
        db.session_factory.cache_clear()
    chosen = replace(source_settings, database_name=name, md_storage_path=storage)
    for module in (auth, db, documents, knowledge, llm, main, observability, operations, resumable_upload,
                   task_queue, worker):
        module.settings = chosen
    return chosen


def category_summary(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[row["category"]].append(row)
    return {name: {"cases": len(group), "passed": sum(bool(item[key]["passed"]) for item in group)}
            for name, group in groups.items() if key in group[0]}


def aggregate_metrics(rows):
    positives = [row["retrieval"] for row in rows if row["expectedEvidence"]]
    times = sorted(row["retrievalMs"] for row in rows)
    return {"evidenceCases": len(positives),
            "candidateRecallAt3": statistics.mean(row["candidateRecall"]["3"] for row in positives),
            "candidateRecallAt5": statistics.mean(row["candidateRecall"]["5"] for row in positives),
            "candidateRecallAt16": statistics.mean(row["candidateRecall"]["16"] for row in positives),
            "finalEvidenceRecall": statistics.mean(row["finalEvidenceRecall"] for row in positives),
            "finalPrecision": statistics.mean(row["finalPrecision"] for row in positives),
            "retrievalP50Ms": statistics.median(times),
            "retrievalP95Ms": times[min(len(times) - 1, math.ceil(.95 * len(times)) - 1)]}


async def run(chosen, *, answers=False, only=None):
    from app import knowledge, main
    from app.auth import hash_password
    from app.db import FileGrant, MarkdownFile, User, db_session
    from app.task_queue import enqueue
    from app.worker import run_once

    corpus = PROJECT_ROOT / "data" / "actastim_s_verified_demo.md"
    destination = chosen.md_storage_path / corpus.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(corpus, destination)
    with db_session() as db:
        admin = User(username="eval_admin", password_hash=hash_password("Eval-only-admin-password-2026"), role="admin")
        granted = User(username="eval_granted", password_hash=hash_password("Eval-only-granted-password-2026"), role="viewer")
        ungranted = User(username="eval_ungranted", password_hash=hash_password("Eval-only-ungranted-password-2026"), role="viewer")
        db.add_all([admin, granted, ungranted])
        db.flush()
        ids = {"admin": admin.id, "granted": granted.id, "ungranted": ungranted.id}
        file = MarkdownFile(original_file_name=corpus.name, new_file_name=corpus.name,
                            file_path=str(destination), file_size=destination.stat().st_size, status=0,
                            owner_id=admin.id, is_published=False, create_time=datetime.now(),
                            update_time=datetime.now())
        db.add(file)
        db.flush()
        file_id = file.id
        db.add(FileGrant(file_id=file_id, user_id=granted.id))
        enqueue(db, file_id)
    assert run_once("quality-eval-worker", file_id=file_id)
    with db_session() as db:
        file = db.get(MarkdownFile, file_id)
        assert file.status == 2, f"评测资料入库失败: {file.processing_error}"
        count = db.scalar(text("SELECT count(*) FROM t_vector_store WHERE metadata->>'mdStorageId'=:id"),
                          {"id": str(file_id)})
        assert count >= len(FACTS), f"入库块数不足: {count}"
        file.is_published = True
    results = []
    clients = {}
    if answers:
        for kind, password in (("granted", "Eval-only-granted-password-2026"),
                               ("ungranted", "Eval-only-ungranted-password-2026")):
            client = TestClient(main.app)
            login = client.post("/auth/login", json={"username": "eval_" + kind, "password": password})
            assert login.status_code == 200
            clients[kind] = (client, {"X-CSRF-Token": login.json()["data"]["csrfToken"]})
    for case in cases():
        if only and case["id"] not in only:
            continue
        if case["principal"] == "draft":
            with db_session() as db:
                db.get(MarkdownFile, file_id).is_published = False
        if case["principal"] == "revoked":
            with db_session() as db:
                db.execute(text("DELETE FROM t_file_grant WHERE file_id=:fid AND user_id=:uid"),
                           {"fid": file_id, "uid": ids["granted"]})
        principal = "ungranted" if case["principal"] == "ungranted" else "granted"
        trace = {}
        started = time.perf_counter()
        found = await knowledge.find_context(case["question"], user_id=ids[principal], role="viewer", trace=trace)
        assert trace["selected"] == found
        duration = (time.perf_counter() - started) * 1000
        scores = retrieval_scores(case, trace)
        entry = {"id": case["id"], "category": case["category"], "question": case["question"],
                 "expectedEvidence": case["evidence"], "retrieval": scores,
                 "retrievalMs": round(duration, 2), "candidateTrace": trace["candidates"],
                 "finalEvidence": found}
        if answers and (case["category"] != "fact" or case["id"].endswith("-1")):
            client, headers = clients[principal]
            response = client.post("/customer-service/completion", json={
                "message": case["question"], "fileId": file_id}, headers=headers)
            if case["principal"] in {"draft", "revoked", "ungranted"}:
                entry["answer"] = {"passed": response.status_code == 404, "status": response.status_code}
            else:
                assert response.status_code == 200, f"SSE HTTP status {response.status_code}: {case['id']}"
                streamed = parse_sse(response.text)
                entry["answer"] = {**answer_check(case, streamed), **streamed}
        results.append(entry)
        print(f"{case['id']}: retrieval={'PASS' if scores['passed'] else 'FAIL'}"
              + (f" answer={'PASS' if entry['answer']['passed'] else 'FAIL'}" if 'answer' in entry else ""), flush=True)
        if case["principal"] == "draft":
            with db_session() as db:
                db.get(MarkdownFile, file_id).is_published = True
        if case["principal"] == "revoked":
            with db_session() as db:
                db.add(FileGrant(file_id=file_id, user_id=ids["granted"]))
    return results, count, hashlib.sha256(corpus.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--answers", action="store_true", help="Also run selected cases through real SSE/model")
    parser.add_argument("--strict", action="store_true", help="Fail exit status on any automated check failure")
    parser.add_argument("--regression", action="store_true",
                        help="Fail on failures beyond the checked-in known retrieval baseline")
    parser.add_argument("--only", action="append", metavar="CASE_ID",
                        help="Run only this case ID; can be repeated for targeted debugging")
    args = parser.parse_args()
    unknown = set(args.only or []) - {case["id"] for case in cases()}
    if unknown:
        parser.error(f"unknown case ID: {sorted(unknown)}")
    if not source_settings.database_url:
        raise SystemExit("独立数据库未配置")
    name = "agent_customer_service_eval_" + uuid4().hex[:12]
    assert re.fullmatch(r"agent_customer_service_eval_[a-f0-9]{12}", name)
    root = (PROJECT_ROOT / "storage" / "quality-eval" / name).resolve()
    assert root.parent == (PROJECT_ROOT / "storage" / "quality-eval").resolve()
    admin_engine = create_engine(make_url(source_settings.database_url).set(database="postgres"),
                                 isolation_level="AUTOCOMMIT")
    created = False
    started = datetime.now().isoformat(timespec="seconds")
    try:
        with admin_engine.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
        created = True
        engine = create_engine(make_url(source_settings.database_url).set(database=name))
        with engine.begin() as conn:
            conn.exec_driver_sql((PROJECT_ROOT / "schema.sql").read_text(encoding="utf-8"))
        engine.dispose()
        chosen = configure(name, root)
        rows, chunks, sha = asyncio.run(run(chosen, answers=args.answers, only=set(args.only or [])))
        retrieval = category_summary(rows, "retrieval")
        answers_summary = category_summary([row for row in rows if "answer" in row], "answer")
        from app import knowledge
        report = {"runAt": started, "corpusSha256": sha, "corpus": "partial verified ActaStim-S demo",
                  "embeddingModel": chosen.embedding_model, "answerModel": chosen.customer_model if args.answers else None,
                  "pipelineConfig": {"chunkTokens": chosen.rag_chunk_tokens, "overlapTokens": chosen.rag_overlap_tokens,
                                     "splitterEnabled": chosen.rag_splitter_enabled,
                                     "candidates": knowledge.CONTEXT_CANDIDATES, "contextLimit": knowledge.CONTEXT_LIMIT,
                                     "maxContextDistance": knowledge.MAX_CONTEXT_DISTANCE},
                  "isolation": "disposable PostgreSQL DB; public corpus indexed fresh; no business DB mutation",
                  "chunks": chunks, "retrieval": retrieval, "answers": answers_summary,
                  "metrics": aggregate_metrics(rows), "cases": rows,
                  "limits": "Labels from one partial public document; keyword checks are conservative; generated answers require human review."}
        output = PROJECT_ROOT / "reports" / "quality"
        output.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = output / f"{stamp}-{name[-12:]}"
        filename.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        lines = ["# 知识库检索质量基线", "", f"运行时间：{started}", f"公开演示语料 SHA-256：`{sha}`",
                 f"入库片段：{chunks}；Embedding：{chosen.embedding_model}；回答模型：{report['answerModel'] or '未运行'}", "",
                 "| 类别 | 检索通过/总数 | 回答自动检查通过/总数 |", "|---|---:|---:|"]
        for key, stats in retrieval.items():
            answer_stats = answers_summary.get(key)
            lines.append(f"| {key} | {stats['passed']}/{stats['cases']} | "
                         + (f"{answer_stats['passed']}/{answer_stats['cases']}" if answer_stats else "未运行") + " |")
        metric = report["metrics"]
        lines += ["", "| 指标 | 当前值 |", "|---|---:|",
                  f"| 候选 Recall@3 | {metric['candidateRecallAt3']:.3f} |",
                  f"| 候选 Recall@5 | {metric['candidateRecallAt5']:.3f} |",
                  f"| 候选 Recall@16 | {metric['candidateRecallAt16']:.3f} |",
                  f"| 最终证据召回率 | {metric['finalEvidenceRecall']:.3f} |",
                  f"| 最终片段精确率 | {metric['finalPrecision']:.3f} |",
                  f"| 检索耗时 P50 / P95 | {metric['retrievalP50Ms']:.1f} / {metric['retrievalP95Ms']:.1f} ms |"]
        lines += ["", "检索通过：指定证据进入最终上下文；无证据/无权限时上下文为空。",
                  "回答自动检查：关键词覆盖、拒答词、SSE 结束和来源。不能代替人工事实核验。",
                  "候选 Recall@3/5/16、最终证据召回率、排序和逐题原始结果见同名 JSON。", "",
                  "## 未通过的题目", ""]
        for row in rows:
            if not row["retrieval"]["passed"] or ("answer" in row and not row["answer"]["passed"]):
                lines.append(f"- {row['id']}：{row['question']}；检索={row['retrieval']['passed']}"
                             + (f"；回答={row['answer']['passed']}" if 'answer' in row else ""))
        lines += ["", "限制：仅一份不完整、公开说明书摘录，未做独立人工盲审或医疗安全性验证；全部结果只适用于当前语料和模型配置。"]
        filename.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"REPORT={filename.with_suffix('.md')}")
        failures = {row["id"] for row in rows if not row["retrieval"]["passed"]}
        answer_failures = {row["id"] for row in rows if "answer" in row and not row["answer"]["passed"]}
        if args.regression:
            baseline = json.loads((PROJECT_ROOT / "evaluation" / "known_failures.json").read_text(encoding="utf-8"))
            unexpected = failures - set(baseline["retrieval"])
            if unexpected or answer_failures:
                print(f"REGRESSION: retrieval={sorted(unexpected)} answer={sorted(answer_failures)}")
                raise SystemExit(1)
            print(f"REGRESSION PASS; known retrieval issue={sorted(failures)}")
        if args.strict and any(not r["retrieval"]["passed"] or ("answer" in r and not r["answer"]["passed"])
                               for r in rows):
            raise SystemExit(1)
    finally:
        from app import db
        if db.session_factory.cache_info().currsize:
            db.session_factory().kw["bind"].dispose()
            db.session_factory.cache_clear()
        if created:
            with admin_engine.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin_engine.dispose()
        if root.exists():
            shutil.rmtree(root)


if __name__ == "__main__":
    main()
