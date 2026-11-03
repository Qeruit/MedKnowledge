"""Import the checked FDA-hosted Xstim PDF into the current knowledge base.

Uses authenticated upload and the independent Worker. New files are submitted
for independent approval; rerun after approval to verify the live SSE answer.
Existing business rows are never deleted or silently published.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.config import PROJECT_ROOT, settings
from app.db import MarkdownFile, db_session
from app.main import app
from scripts.auth_helpers import login_client


def answer(response):
    response.raise_for_status()
    assert "event: sources" in response.text and "event: close" in response.text, response.text[:500]
    lines = response.text.splitlines()
    sources = [json.loads(line[6:]) for line in lines if line.startswith("data: ")]
    return "".join(item.get("v", "") for item in sources), [item for item in sources if "items" in item]


def main():
    info = json.loads((PROJECT_ROOT / "data/public_manuals/Xstim_P230025D.source.json").read_text(encoding="utf-8"))
    source = PROJECT_ROOT / "data/public_manuals" / info["file"]
    expected_hash = info["sha256"]
    if hashlib.sha256(source.read_bytes()).hexdigest() != expected_hash:
        raise RuntimeError("公开手册的 SHA-256 不匹配，停止导入")
    with db_session() as db:
        existing = db.scalars(select(MarkdownFile).where(MarkdownFile.original_file_name == source.name)).all()
        if len(existing) > 1:
            raise RuntimeError(f"同名资料存在多条（ID: {[item.id for item in existing]}），停止以避免误用")
        if existing:
            row = existing[0]
            stored = Path(row.file_path)
            if not stored.is_file() or hashlib.sha256(stored.read_bytes()).hexdigest() != expected_hash:
                raise RuntimeError(f"已有资料 ID {row.id} 的文件内容不一致，停止以避免覆盖")
            if row.status != 2:
                raise RuntimeError(f"已有资料 ID {row.id} 尚未处理完成，先检查任务状态")
            file_id = row.id
        else:
            file_id = None

    client = TestClient(app)
    headers = login_client(client)
    try:
        if file_id is None:
            with source.open("rb") as stream:
                result = client.post("/customer-service/md/upload", files={"file": (source.name, stream, "application/pdf")},
                                     headers=headers)
            result.raise_for_status()
            if not result.json().get("success"):
                raise RuntimeError(f"上传失败：{result.json().get('message')}")
            file_id = result.json()["data"]["id"]
            subprocess.run([sys.executable, "-c",
                            "from app.worker import run_once; assert run_once(worker_id='xstim-import', file_id="
                            + str(int(file_id)) + ")"], cwd=PROJECT_ROOT, check=True,
                           timeout=settings.task_timeout_seconds + 60)
        with db_session() as db:
            row = db.get(MarkdownFile, file_id)
            if row.status != 2:
                raise RuntimeError(f"入库任务失败：{row.processing_error}")
            pages = db.execute(text("""SELECT DISTINCT (metadata->>'pdfPage')::int FROM t_vector_store
                                       WHERE metadata->>'mdStorageId'=:id ORDER BY 1"""),
                               {"id": str(file_id)}).scalars().all()
            chunk_count = db.scalar(text("""SELECT count(*) FROM t_vector_store
                                          WHERE metadata->>'mdStorageId'=:id"""), {"id": str(file_id)})
            if pages != list(range(1, info["pdfPages"] + 1)):
                raise RuntimeError(f"页码不完整：{pages}")
            db.execute(text("""UPDATE t_vector_store
                               SET metadata = (metadata::jsonb || jsonb_build_object('sourceUrl', CAST(:url AS text)))::json
                               WHERE metadata->>'mdStorageId'=:id"""),
                       {"id": str(file_id), "url": info["sourceUrl"]})
        update = client.post("/customer-service/md/update", json={
            "id": file_id, "remark": "公开来源：" + info["sourceUrl"]}, headers=headers)
        update.raise_for_status()
        if not update.json().get("success"):
            raise RuntimeError(f"来源备注更新失败：{update.json().get('message')}")
        with db_session() as db:
            row = db.get(MarkdownFile, file_id)
            registered, published, review_status = row.document_key is not None, row.is_published, row.review_status
        if not registered:
            result = client.post("/customer-service/md/versions/register", json={
                "id": file_id, "revisionLabel": "FDA public manual", "sourceUrl": info["sourceUrl"]}, headers=headers)
            result.raise_for_status()
            registered = True
            review_status = result.json()["data"]["reviewStatus"]
        if not published:
            if review_status in ("draft", "rejected"):
                client.post("/customer-service/md/versions/submit", json={"id": file_id}, headers=headers).raise_for_status()
            print(json.dumps({"fileId": file_id, "status": "pending_review",
                              "nextStep": "请由不同账号的系统管理员批准后重跑脚本验证 SSE"}, ensure_ascii=False))
            return

        checks = []
        for question, expected in [
            ("Xstim 的存储和运输温度是多少？", ("-29", "60")),
            ("Xstim 推荐每天治疗多少小时，治疗计划最多多少天？", ("24", "270")),
        ]:
            reply, source_events = answer(client.post("/customer-service/completion", json={"message": question, "fileId": file_id}, headers=headers))
            cards = [card for event in source_events for card in event["items"]]
            if not cards or not any(card.get("fileName") == source.name for card in cards):
                raise RuntimeError(f"回答没有命中新手册：{question}")
            if not all(card.get("sourceUrl") == info["sourceUrl"] for card in cards):
                raise RuntimeError(f"来源卡片缺少官方 PDF 链接：{question}")
            if not all(term in reply for term in expected):
                raise RuntimeError(f"回答缺少已核对数值：{question}；实际回答：{reply[:500]}")
            if "存储和运输温度" in question:
                if not any("第 3 页" in card.get("citation", "") for card in cards):
                    raise RuntimeError("存储与运输温度的原文证据页（PDF 第 3 页）未被引用")
                if "存储" not in reply or re.search(r"未.{0,12}(单独|明确|列出|提供)|资料不足|未知", reply):
                    raise RuntimeError(f"存储温度回答仍包含误导性缺失表述：{reply[:500]}")
            checks.append({"question": question, "expected": expected, "sources": len(cards),
                           "answer": reply[:500], "passed": True})
        other_device = client.post("/customer-service/completion", json={
            "message": "ActaStim-S 的存储温度是多少？", "fileId": file_id}, headers=headers)
        other_device.raise_for_status()
        if "event: sources" in other_device.text or "不足以回答" not in other_device.text:
            raise RuntimeError("其他设备问题错误地引用了 Xstim 手册")
        checks.append({"question": "ActaStim-S 的存储温度是多少？", "expected": "refusal",
                       "sources": 0, "passed": True})
        print(json.dumps({"fileId": file_id, "name": source.name, "pdfPages": len(pages),
                          "chunks": chunk_count, "status": "completed", "published": True,
                          "questions": checks}, ensure_ascii=False))
    finally:
        client.post("/auth/logout")
        client.close()


if __name__ == "__main__":
    main()
