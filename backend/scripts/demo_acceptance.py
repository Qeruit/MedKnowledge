"""Read-only business acceptance over the real HTTP proxy and SSE.

Uses already published, authorized public ActaStim-S demo material. Creates
test conversations, but never uploads, approves, grants or deletes documents.
Passwords and answer bodies are never written to the acceptance report.
"""
import argparse
from datetime import datetime
import getpass
import json
from pathlib import Path
import time

import httpx


def normalized(value):
    return value.replace("−", "-").replace("–", "-")


def ask(client, question, file_id, conversation_id=None):
    started = time.perf_counter()
    event, lines, answer, sources = "", [], [], []
    conversation, closed, first_ms = None, False, None
    with client.stream("POST", "/api/customer-service/completion", json={
            "message": question, "fileId": file_id, "conversationId": conversation_id},
            timeout=httpx.Timeout(190, connect=10)) as response:
        response.raise_for_status()
        if not response.headers.get("content-type", "").startswith("text/event-stream"):
            raise RuntimeError("Response is not SSE")
        for line in response.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                lines.append(line[5:].strip())
            elif not line and lines:
                body = json.loads("\n".join(lines))
                if event == "conversation":
                    conversation = body["id"]
                elif event == "sources":
                    sources.extend(body.get("items", []))
                elif event == "close":
                    closed = True
                elif event == "error":
                    raise RuntimeError("Server returned SSE error")
                elif body.get("v"):
                    answer.append(body["v"])
                    if first_ms is None:
                        first_ms = round((time.perf_counter() - started) * 1000)
                event, lines = "", []
    if lines:
        raise RuntimeError("Incomplete SSE frame")
    return {"answer": normalized("".join(answer)), "sources": sources, "conversation": conversation,
            "closed": closed, "chunks": len(answer), "firstChunkMs": first_ms,
            "elapsedMs": round((time.perf_counter() - started) * 1000)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-file", help="Private local bootstrap JSON, never included in report")
    parser.add_argument("--file-id", type=int)
    parser.add_argument("--second-file-id", type=int, help="Optional second already-published authorized file")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        parser.error("base URL must use http:// or https://")
    if args.password_file:
        credentials = json.loads(Path(args.password_file).read_text(encoding="utf-8"))
        if credentials.get("username") != args.username:
            parser.error("credential username mismatch")
        password = credentials["password"]
    else:
        password = getpass.getpass(f"Password for {args.username}: ")
    results = []

    def check(name, passed, **detail):
        results.append({"name": name, "status": "PASS" if passed else "FAIL", **detail})
        print(f"{name}: {'PASS' if passed else 'FAIL'}", flush=True)

    report = {"runAt": datetime.now().astimezone().isoformat(timespec="seconds"), "baseUrl": base,
              "scope": "real HTTP/SSE + persistent history; browser interactions checked separately",
              "cases": results}
    with httpx.Client(base_url=base, headers={"Origin": base}, timeout=30) as client:
        logged_in = False
        try:
            ready = client.get("/api/ready")
            check("readiness", ready.status_code == 200 and ready.json().get("status") == "ready")
            response = client.post("/api/auth/login", json={"username": args.username, "password": password})
            response.raise_for_status()
            client.headers["X-CSRF-Token"] = response.json()["data"]["csrfToken"]
            logged_in = True
            check("login", True)
            response = client.get("/api/customer-service/md/available")
            response.raise_for_status()
            available = response.json()["data"]
            file = next((f for f in available if f["id"] == args.file_id), None) if args.file_id else next(
                (f for f in available if f["fileName"] == "actastim_s_verified_demo.md"), None)
            if not file or file["fileName"] != "actastim_s_verified_demo.md":
                raise RuntimeError("Published authorized ActaStim-S public demo required; no release changes made")
            fid = file["id"]
            report["fileId"] = fid
            check("published_document_available", True)
            fact = ask(client, "ActaStim-S 的存储温度是多少？", fid)
            check("fact_and_citations", fact["closed"] and bool(fact["conversation"]) and bool(fact["sources"])
                  and "-25" in fact["answer"] and "70" in fact["answer"]
                  and all(s.get("fileName") == file["fileName"] for s in fact["sources"]),
                  chunks=fact["chunks"], sources=len(fact["sources"]), firstChunkMs=fact["firstChunkMs"])
            follow = ask(client, "那它每天推荐佩戴多久，设计最长使用期是多少天？", fid, fact["conversation"])
            check("followup_same_conversation", follow["closed"] and follow["conversation"] == fact["conversation"]
                  and all(t in follow["answer"] for t in ("24", "270")) and bool(follow["sources"]))
            history = client.get(f"/api/customer-service/conversations/{fact['conversation']}")
            history.raise_for_status()
            turns = history.json()["data"]["turns"]
            check("history_for_refresh", len(turns) == 2 and all(t["answer"] and t["sources"] for t in turns))
            refusal = ask(client, "请用 Python 写冒泡排序代码", fid, fact["conversation"])
            check("unsupported_question", refusal["closed"] and not refusal["sources"] and "不足以回答" in refusal["answer"])
            new = ask(client, "ActaStim-S 的存储温度是多少？", fid)
            check("new_conversation", new["closed"] and bool(new["conversation"]) and new["conversation"] != fact["conversation"])
            bad = client.post("/api/customer-service/completion", json={"message": "", "fileId": fid})
            check("empty_input_rejected", bad.status_code == 422)
            second = next((f for f in available if f["id"] == args.second_file_id and f["id"] != fid), None)
            if second:
                bad = client.post("/api/customer-service/completion", json={
                    "message": "资料中的信息是什么？", "fileId": second["id"], "conversationId": fact["conversation"]})
                check("cross_document_conversation_rejected", bad.status_code == 409)
            else:
                results.append({"name": "cross_document_conversation_rejected", "status": "NOT_RUN",
                                "reason": "Provide --second-file-id for a second already-published authorized document"})
            with httpx.Client(base_url=base, timeout=15) as anonymous:
                unauth = anonymous.get(f"/api/customer-service/conversations/{fact['conversation']}")
                check("anonymous_history_rejected", unauth.status_code == 401)
        except Exception as exc:
            results.append({"name": "run_error", "status": "FAIL", "errorType": type(exc).__name__})
            print(f"Acceptance stopped: {type(exc).__name__}; inspect server logs without sharing secrets", flush=True)
        finally:
            if logged_in:
                try:
                    response = client.post("/api/auth/logout")
                    check("logout", response.status_code == 200)
                except httpx.HTTPError:
                    check("logout", False)
    output = Path(__file__).resolve().parents[1] / "reports/demo"
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report["passed"] = not any(c["status"] == "FAIL" for c in results)
    report["complete"] = report["passed"] and not any(c["status"] == "NOT_RUN" for c in results)
    target = output / f"{stamp}-http-acceptance"
    target.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 演示 HTTP 验收", "", f"时间：{report['runAt']}",
             "不上传、批准或删除资料；测试会生成问答历史。密码、Cookie 和回答正文不写入报告。", "",
             "| 检查 | 结果 |", "|---|---|"]
    lines += [f"| {c['name']} | {c['status']} |" for c in results]
    lines += ["", "NOT_RUN 表示前提未具备，不能当作通过。此脚本不代替浏览器验收。"]
    target.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"REPORT={target.with_suffix('.md')}")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
