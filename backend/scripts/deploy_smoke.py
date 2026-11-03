"""Exercise the deployed reverse proxy, login and SSE without exposing answers.

Run from the host, not inside the backend container. The prompt must be a
harmless test question about published, non-sensitive demonstration material.
"""

import argparse
import getpass
import json
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--origin", help="Browser Origin when testing an internal proxy hostname")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-file", help="Local bootstrap JSON; for isolated automation only")
    parser.add_argument("--question", required=True)
    parser.add_argument("--file-id", type=int, help="要检索的已发布资料 ID；可查询多份资料时必须指定")
    parser.add_argument("--ca", help="Trusted CA/certificate for a non-public HTTPS test endpoint")
    parser.add_argument("--expect-sources", action="store_true")
    parser.add_argument("--min-chunks", type=int, default=1)
    args = parser.parse_args()
    if args.min_chunks < 1:
        parser.error("--min-chunks must be at least 1")
    base_url = args.base_url.rstrip("/")
    origin = args.origin or base_url
    if not base_url.startswith(("http://", "https://")) or not origin.startswith(("http://", "https://")):
        parser.error("--base-url must start with http:// or https://")

    if args.password_file:
        credential = json.loads(Path(args.password_file).read_text(encoding="utf-8"))
        if credential.get("username") != args.username:
            parser.error("bootstrap credential username differs from --username")
        password = credential["password"]
    else:
        password = getpass.getpass(f"Password for {args.username}: ")
    verify = args.ca if args.ca else True
    with httpx.Client(base_url=base_url, verify=verify, timeout=15) as client:
        health = client.get("/healthz")
        health.raise_for_status()
        if health.text.strip() != "ok":
            raise RuntimeError("反向代理健康检查异常")
        ready = client.get("/api/ready")
        ready.raise_for_status()
        if ready.json().get("status") != "ready":
            raise RuntimeError("API 未就绪")
        login = client.post("/api/auth/login", json={"username": args.username, "password": password},
                            headers={"Origin": origin})
        login.raise_for_status()
        payload = login.json()
        if not payload.get("success") or not payload.get("data", {}).get("csrfToken"):
            raise RuntimeError("登录未返回有效 CSRF Token")
        available = client.get("/api/customer-service/md/available")
        available.raise_for_status()
        documents = available.json()["data"]
        if args.file_id is None and len(documents) != 1:
            raise RuntimeError("请用 --file-id 选择一份可查询资料；当前可查询份数：" + str(len(documents)))
        file_id = args.file_id if args.file_id is not None else documents[0]["id"]
        if file_id not in {document["id"] for document in documents}:
            raise RuntimeError("--file-id 指向的资料不存在或当前账户无权查询")

        started = time.perf_counter()
        chunk_count = source_count = 0
        first_chunk_ms = None
        closed = False
        current_event = ""
        data_lines = []
        with client.stream("POST", "/api/customer-service/completion",
                           json={"message": args.question, "fileId": file_id},
                           headers={"Origin": origin, "X-CSRF-Token": payload["data"]["csrfToken"]},
                           timeout=httpx.Timeout(190, connect=10)) as response:
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("text/event-stream"):
                raise RuntimeError("回答不是 SSE 响应")
            for line in response.iter_lines():
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line == "":
                    if data_lines:
                        event_payload = json.loads("\n".join(data_lines))
                        if current_event == "sources":
                            source_count = len(event_payload.get("items", []))
                        elif current_event == "error":
                            raise RuntimeError("模型或检索返回 SSE error；请检查服务端日志")
                        elif current_event == "close":
                            closed = True
                        elif isinstance(event_payload.get("v"), str) and event_payload["v"]:
                            chunk_count += 1
                            if first_chunk_ms is None:
                                first_chunk_ms = round((time.perf_counter() - started) * 1000)
                    current_event = ""
                    data_lines.clear()

    if not closed or chunk_count < args.min_chunks:
        raise RuntimeError("SSE 未完整结束或可见回答分块数不足")
    if args.expect_sources and source_count == 0:
        raise RuntimeError("未检索到预期的已发布资料；检查授权和知识入库状态")
    print(json.dumps({"ready": True, "sseClosed": closed, "sources": source_count,
                      "answerChunks": chunk_count, "firstChunkMs": first_chunk_ms,
                      "totalMs": round((time.perf_counter() - started) * 1000)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
