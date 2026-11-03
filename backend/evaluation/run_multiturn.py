"""Isolated multi-turn retrieval regression; add --answers for real SSE/model checks.

Usage: python -m evaluation.run_multiturn [--answers] [--strict]
Creates and removes one disposable PostgreSQL database. Never writes business data.
Reports contain public demonstration questions, retrieved passages and (optionally)
model answers; do not use this runner with confidential or patient materials.
"""

import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import statistics
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.chat_history import contextualize_question
from app.config import PROJECT_ROOT, settings as source_settings
from evaluation.multiturn_cases import SCENARIOS
from evaluation.quality import answer_check, parse_sse, retrieval_scores
from evaluation.run_quality import configure


def conversation_ids(sse_text: str) -> list[str]:
    ids = []
    for block in sse_text.replace("\r\n", "\n").split("\n\n"):
        if not any(line.strip() == "event: conversation" for line in block.splitlines()):
            continue
        for line in block.splitlines():
            if line.startswith("data:"):
                ids.append(json.loads(line[5:].strip())["id"])
    return ids


def source_coverage(gold: list[str], sources: list[dict]) -> bool:
    if not gold:
        return not sources
    return all(any(anchor in source.get("heading", "") for source in sources) for anchor in gold)


async def evaluate(chosen, *, answers: bool) -> tuple[list[dict], int, str]:
    from app import knowledge, main
    from app.auth import hash_password
    from app.db import FileGrant, MarkdownFile, User, db_session
    from app.task_queue import enqueue
    from app.worker import run_once

    corpus = PROJECT_ROOT / "data" / "actastim_s_verified_demo.md"
    destination = chosen.md_storage_path / corpus.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(corpus, destination)
    password = "Disposable-eval-only-password-2026"
    with db_session() as db:
        admin = User(username="multi_eval_admin", password_hash=hash_password(password), role="admin")
        viewer = User(username="multi_eval_viewer", password_hash=hash_password(password), role="viewer")
        db.add_all([admin, viewer])
        db.flush()
        viewer_id = viewer.id
        file = MarkdownFile(original_file_name=corpus.name, new_file_name=corpus.name,
                            file_path=str(destination), file_size=destination.stat().st_size,
                            status=0, owner_id=admin.id, is_published=False,
                            create_time=datetime.now(), update_time=datetime.now())
        db.add(file)
        db.flush()
        file_id = file.id
        db.add(FileGrant(file_id=file_id, user_id=viewer_id))
        enqueue(db, file_id)
    assert run_once("multi-eval-worker", file_id=file_id)
    with db_session() as db:
        file = db.get(MarkdownFile, file_id)
        assert file.status == 2, f"评测资料入库失败: {file.processing_error}"
        chunk_count = db.scalar(text("SELECT count(*) FROM t_vector_store WHERE metadata->>'mdStorageId'=:id"),
                                {"id": str(file_id)})
        file.is_published = True

    client = None
    headers = None
    if answers:
        client = TestClient(main.app)
        login = client.post("/auth/login", json={"username": "multi_eval_viewer", "password": password})
        assert login.status_code == 200, "评测查询者登录失败"
        headers = {"X-CSRF-Token": login.json()["data"]["csrfToken"]}

    results = []
    first_conversation_id = None
    for scenario in SCENARIOS:
        prior_questions = []
        conversation_id = None
        turn_results = []
        for turn in scenario["turns"]:
            question = turn["question"]
            retrieval_question, reference = contextualize_question(question, prior_questions[-4:])
            trace = {}
            started = time.perf_counter()
            documents = await knowledge.find_context(retrieval_question, user_id=viewer_id, role="viewer",
                                                      file_id=file_id, trace=trace)
            retrieval_ms = round((time.perf_counter() - started) * 1000, 2)
            score = retrieval_scores(turn, trace)
            entry = {"question": question, "retrievalQuestion": retrieval_question,
                     "referenceQuestion": reference, "goldEvidence": turn["evidence"],
                     "retrievalMs": retrieval_ms, "retrieval": score,
                     "selectedEvidence": [doc["content"].splitlines()[0] for doc in documents],
                     "candidateTrace": trace["candidates"]}
            if answers:
                payload = {"message": question, "fileId": file_id}
                if conversation_id:
                    payload["conversationId"] = conversation_id
                response = client.post("/customer-service/completion", json=payload, headers=headers)
                assert response.status_code == 200, f"{scenario['id']} SSE HTTP {response.status_code}"
                assert response.headers["content-type"].startswith("text/event-stream")
                ids = conversation_ids(response.text)
                assert len(ids) == 1 and (conversation_id is None or conversation_id == ids[0]), \
                    f"{scenario['id']} 会话 ID 不连续"
                conversation_id = ids[0]
                streamed = parse_sse(response.text)
                checked = answer_check(turn, streamed)
                checked["modelAnswerPassed"] = checked["passed"]
                checked["sourceCoverage"] = source_coverage(turn["evidence"], streamed["sources"])
                checked["passed"] = checked["modelAnswerPassed"] and checked["sourceCoverage"]
                entry["answer"] = {**checked, **streamed}
            turn_results.append(entry)
            prior_questions.append(question)
            print(f"{scenario['id']} turn {len(turn_results)}: retrieval={'PASS' if score['passed'] else 'FAIL'}"
                  + (f" model={'PASS' if entry['answer']['modelAnswerPassed'] else 'FAIL'}"
                     f" sources={'PASS' if entry['answer']['sourceCoverage'] else 'FAIL'}"
                     if answers else ""), flush=True)
        if answers:
            detail = client.get(f"/customer-service/conversations/{conversation_id}")
            assert detail.status_code == 200
            saved = detail.json()["data"]
            assert saved["fileId"] == file_id
            assert [item["question"] for item in saved["turns"]] == prior_questions
            assert all(item["answer"] for item in saved["turns"])
            first_conversation_id = first_conversation_id or conversation_id
        results.append({"id": scenario["id"], "category": scenario["category"],
                        "conversationRestored": bool(answers), "turns": turn_results})

    if answers:
        # Session scope is a separate protocol check: another published file
        # must never inherit this conversation, even for the same user.
        with db_session() as db:
            second = MarkdownFile(original_file_name="scope-only.md", new_file_name="scope-only.md",
                                  file_path=str(destination), file_size=destination.stat().st_size,
                                  status=2, owner_id=db.get(User, viewer_id).id, is_published=True,
                                  create_time=datetime.now(), update_time=datetime.now())
            db.add(second)
            db.flush()
            second_id = second.id
        rejected = client.post("/customer-service/completion", json={
            "message": "那它每天使用多久？", "fileId": second_id,
            "conversationId": first_conversation_id}, headers=headers)
        assert rejected.status_code == 409, f"跨资料复用会话应返回 409，实际 {rejected.status_code}"
        print("file-switch: PASS (HTTP 409)", flush=True)
    return results, chunk_count, hashlib.sha256(corpus.read_bytes()).hexdigest()


def summarize(results: list[dict], answers: bool) -> dict:
    turns = [turn for scenario in results for turn in scenario["turns"]]
    positives = [turn for turn in turns if turn["goldEvidence"]]
    negatives = [turn for turn in turns if not turn["goldEvidence"]]
    values = [turn["retrievalMs"] for turn in turns]
    return {
        "scenarios": len(results), "turns": len(turns),
        "retrievalPassed": sum(turn["retrieval"]["passed"] for turn in turns),
        "modelAnswerPassed": sum(turn["answer"]["modelAnswerPassed"] for turn in turns) if answers else None,
        "sourceCoveragePassed": sum(turn["answer"]["sourceCoverage"] for turn in turns) if answers else None,
        "endToEndPassed": sum(turn["answer"]["passed"] for turn in turns) if answers else None,
        "candidateRecallAt5": statistics.mean(turn["retrieval"]["candidateRecall"]["5"] for turn in positives),
        "finalEvidenceRecall": statistics.mean(turn["retrieval"]["finalEvidenceRecall"] for turn in positives),
        "finalPrecision": statistics.mean(turn["retrieval"]["finalPrecision"] for turn in positives),
        "noEvidenceFalsePositiveRate": sum(not turn["retrieval"]["passed"] for turn in negatives) / len(negatives),
        "retrievalP50Ms": statistics.median(values),
        "retrievalP95Ms": sorted(values)[max(0, (len(values) * 95 + 99) // 100 - 1)],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--answers", action="store_true", help="Call real model through SSE and check stored turns")
    parser.add_argument("--strict", action="store_true", help="Return nonzero when a gold check fails")
    parser.add_argument("--regression", action="store_true", help="Fail on new failures beyond the known baseline")
    args = parser.parse_args()
    if not source_settings.database_url:
        raise SystemExit("独立数据库未配置")
    name = "agent_customer_service_multi_eval_" + uuid4().hex[:12]
    assert re.fullmatch(r"agent_customer_service_multi_eval_[a-f0-9]{12}", name)
    root = (PROJECT_ROOT / "storage" / "quality-eval" / name).resolve()
    assert root.parent == (PROJECT_ROOT / "storage" / "quality-eval").resolve()
    admin_engine = create_engine(make_url(source_settings.database_url).set(database="postgres"),
                                 isolation_level="AUTOCOMMIT")
    created = False
    try:
        with admin_engine.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
        created = True
        engine = create_engine(make_url(source_settings.database_url).set(database=name))
        with engine.begin() as conn:
            conn.exec_driver_sql((PROJECT_ROOT / "schema.sql").read_text(encoding="utf-8"))
        engine.dispose()
        chosen = configure(name, root)
        rows, chunks, sha = asyncio.run(evaluate(chosen, answers=args.answers))
        metrics = summarize(rows, args.answers)
        report = {"runAt": datetime.now().isoformat(timespec="seconds"),
                  "corpus": "partial verified ActaStim-S public demo", "corpusSha256": sha,
                  "embeddingModel": chosen.embedding_model,
                  "answerModel": chosen.customer_model if args.answers else None,
                  "isolation": "disposable PostgreSQL DB; indexed fresh; no business DB mutation",
                  "chunks": chunks, "metrics": metrics, "scenarios": rows,
                  "limits": "Small public corpus; keyword answer checks are not clinical accuracy or human review."}
        output = PROJECT_ROOT / "reports" / "quality"
        output.mkdir(parents=True, exist_ok=True)
        filename = output / f"{datetime.now():%Y%m%d-%H%M%S}-multiturn-{name[-12:]}"
        filename.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                                                 encoding="utf-8")
        retrieval_failures = {f"{scenario['id']}#{i}" for scenario in rows
                              for i, turn in enumerate(scenario["turns"], 1) if not turn["retrieval"]["passed"]}
        end_to_end_failures = {f"{scenario['id']}#{i}" for scenario in rows
                               for i, turn in enumerate(scenario["turns"], 1)
                               if args.answers and not turn["answer"]["passed"]}
        failed = sorted(retrieval_failures | end_to_end_failures)
        lines = ["# 多轮追问质量回归", "", f"演示语料 SHA-256：`{sha}`；入库片段：{chunks}",
                 f"模型回答：{'已运行真实 SSE' if args.answers else '未运行（仅检索）'}", "",
                 f"检索通过：{metrics['retrievalPassed']}/{metrics['turns']}；"
                 + (f"模型文字自动检查通过：{metrics['modelAnswerPassed']}/{metrics['turns']}；"
                    f"来源覆盖通过：{metrics['sourceCoveragePassed']}/{metrics['turns']}；"
                    f"端到端通过：{metrics['endToEndPassed']}/{metrics['turns']}"
                    if args.answers else "回答未运行"),
                 f"候选 Recall@5：{metrics['candidateRecallAt5']:.3f}；"
                 f"最终证据召回：{metrics['finalEvidenceRecall']:.3f}；"
                 f"最终片段精确率：{metrics['finalPrecision']:.3f}",
                 f"无证据误召回率：{metrics['noEvidenceFalsePositiveRate']:.3f}；"
                 f"检索 P50/P95：{metrics['retrievalP50Ms']:.1f}/{metrics['retrievalP95Ms']:.1f} ms", "",
                 "未通过：" + ("、".join(failed) if failed else "无"), "",
                 "逐轮检索候选、证据及自动回答核验见同名 JSON；自动检查不能代替人工事实核验。"]
        filename.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"REPORT={filename.with_suffix('.md')}")
        if args.regression:
            baseline = json.loads((PROJECT_ROOT / "evaluation" / "multiturn_known_failures.json")
                                  .read_text(encoding="utf-8"))
            unexpected_retrieval = retrieval_failures - set(baseline["retrieval"])
            unexpected_end_to_end = end_to_end_failures - set(baseline["endToEnd"])
            if unexpected_retrieval or unexpected_end_to_end:
                print(f"REGRESSION: retrieval={sorted(unexpected_retrieval)} "
                      f"endToEnd={sorted(unexpected_end_to_end)}")
                raise SystemExit(1)
            print(f"REGRESSION PASS; known issue={failed}")
        if args.strict and failed:
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
