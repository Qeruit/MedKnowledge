"""End-to-end test against the standalone local API and dedicated database.

Uses only the verified public demo Markdown and does not delete records.
"""

import json
import time

import httpx
from sqlalchemy import create_engine, text

from app.config import PROJECT_ROOT, settings
from scripts.auth_helpers import login_client

BASE = "http://127.0.0.1:8010"
CLIENT = httpx.Client(base_url=BASE, timeout=30)
DEMO = PROJECT_ROOT / "data" / "actastim_s_verified_demo.md"


def request(path: str, payload: dict):
    response = CLIENT.post(path, json=payload, timeout=30)
    response.raise_for_status()
    body = response.json()
    if not body["success"]:
        raise AssertionError(body["message"])
    return body


def sse(question: str, file_id: int):
    with CLIENT.stream("POST", "/customer-service/completion",
                      json={"message": question, "fileId": file_id}, timeout=180) as response:
        response.raise_for_status()
        return response.read().decode("utf-8")


def visible_answer(stream: str) -> str:
    return "".join(
        json.loads(line[6:])["v"]
        for line in stream.splitlines()
        if line.startswith("data: ") and '"v"' in line
    )


def main():
    login_client(CLIENT)
    assert httpx.get(BASE + "/ready", timeout=5).json()["status"] == "ready"
    assert httpx.post(BASE + "/customer-service/md/list", json={}).status_code == 401
    rows = request("/customer-service/md/list", {"current": 1, "size": 20})["data"]
    record = next((item for item in rows if item["originalFileName"] == DEMO.name), None)
    if record is None:
        with DEMO.open("rb") as file:
            response = CLIENT.post("/customer-service/md/upload",
                                  files={"file": (DEMO.name, file, "text/markdown")},
                                  timeout=120)
        response.raise_for_status()
        body = response.json()
        assert body["success"], body
        file_id = body["data"]["id"]
        print(f"Uploaded verified public demo as file #{file_id}")
    else:
        file_id = record["id"]
        print(f"Using existing public demo file #{file_id}")

    for _ in range(45):
        rows = request("/customer-service/md/list", {"current": 1, "size": 20})["data"]
        record = next((item for item in rows if item["id"] == file_id), None)
        if record and record["status"] in (2, 3):
            break
        time.sleep(2)
    assert record and record["status"] == 2, f"Index status: {record}"
    assert record["isPublished"], "请在管理页发布演示资料，再运行问答烟测；脚本不会自动发布已有资料"
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        info = conn.execute(text("""
            SELECT count(*) AS n,
                   min(vector_dims(embedding)) AS min_dim,
                   max(vector_dims(embedding)) AS max_dim,
                   count(*) FILTER (WHERE metadata ->> 'sourceUrl' IS NOT NULL) AS sourced
            FROM t_vector_store WHERE metadata ->> 'mdStorageId' = :id
        """), {"id": str(file_id)}).one()
    assert info.n > 0 and info.min_dim == info.max_dim == settings.embedding_dimensions
    assert info.sourced == info.n
    print(f"Indexed {info.n} vectors, all {info.min_dim}-dimensional, all with source URL")

    answer = sse("ActaStim-S 的存储温度是多少？治疗多久？", file_id)
    assert "event: sources" in answer and "event: close" in answer and "event: error" not in answer, answer[:600]
    assert "https://www.accessdata.fda.gov/cdrh_docs/pdf19/P190030C.pdf" in answer
    text_answer = visible_answer(answer)
    assert all(fact in text_answer for fact in ("-25", "70", "24", "270")), text_answer
    print("Combined factual question: model answer contains both supported facts and source evidence")
    outside = sse("请用 Python 写一个冒泡排序程序", file_id)
    assert "不足以回答" in visible_answer(outside) and "event: close" in outside and "event: sources" not in outside
    print("Out-of-domain refusal: verified")
    stats = CLIENT.get("/customer-service/md/stats", timeout=5).json()
    audit = CLIENT.get("/customer-service/md/audit", timeout=5).json()
    assert stats["success"] and audit["success"]
    print("Admin statistics and audit: verified")


if __name__ == "__main__":
    try:
        main()
    finally:
        CLIENT.post("/auth/logout")
        CLIENT.close()
