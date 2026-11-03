"""Record a bounded, explicitly AI-authored technical review; never publish.

The decisions below follow visual inspection of the immutable source and the
exported live index. This is an operator script, NOT an automatic approval rule
for other documents. Dry-run by default; no user credentials or impersonation.
"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace


SOURCE = "c3fa82d25e335dc9beb39f371b9c47984812936029b00dbe5cb7a4c8ab03bb40"
REPAIRED = {
    16: "电池状态、三短声/四中声及12秒关机逐行对应；图标仅为文字描述，不支持图像识别。",
    17: "E315到期、E3XX故障、不支持充电器三行对应；凝胶和重启步骤未移用于到期状态。",
    18: "运行/运输/储存三种条件、温湿度/气压及拆包装后限定对应原页。",
    21: "电源型号、各项电气和声学参数、容差及厂家旁注对应；寿命未改写为疗程。",
    24: "发射/抗扰度分表；Group 1/Class B、共享单元格、ESD及快速瞬变参数对应原页。",
    25: "浪涌、电压跌落与15组RF频率/场强逐行对应；保留UT定义及环境限定。",
    31: "6组分类、行值、共享p值及脚注对应原表；250dpi复核区间确为256-356天，未擅改为365天。仅转写核对，不作个体疗效预测。",
    32: "骨类型、长骨分组、移位及骨干部位四组逐行对应；缺失数保留括号，未补算百分比。",
    33: "术式No/Yes分别绑定数值；吸烟、假关节等分组及共享p值对应；不作个体疗效预测。",
    50: "西语原页电池表与前接RF段均保留；状态/音响/动作对应；英文规范字段非官方译本。",
    51: "西语原页三种故障状态独立对应，未串用处理步骤；英文规范字段非官方译本。",
    52: "西语运行/运输/储存数值及去包装限定对应；英文规范表头非官方译本。",
    55: "西语电源型号/电气表与声学数值/容差对应；保留厂家旁注；非官方译本。",
    58: "西语发射表及ESD参数对应；快速瞬变在物理59页，未错误合并进58页。",
    59: "西语快速瞬变、浪涌、电压跌落及15组RF频率/场强逐行对应；保留UT定义。",
    65: "西语6组统计行值、p值及脚注对应；<82天行恢复9/9/0/100%；仅原表转写核对。",
    66: "西语骨类型逐行核对，Tibia为28/26/2/93%，Femur为13/12/1/92%；分组与缺失数保留。",
    67: "西语术式No/Sí、吸烟及假关节组别逐行对应；括号缺失数和共享p值保留。",
}
TEXT_ONLY = {
    6: "凝胶1-2mm、无字面及仅原配电源等正文对应；图2/3照片不属于本次文字索引验收能力。",
    7: "每日20分钟、最多365天、医生决定疗程及has not been established限定和MR警告保留；只确认文字转写，不确认临床适用性。",
    13: "清洁前关机断电、步骤1-4及禁止直接喷洒/倾倒液体对应；不提供正文以外的照片指导。",
    40: "西语凝胶厚度、涂抹侧与原配电源正文对应；仅文字转写，非图像操作验收。",
    41: "西语20分钟/天、最多365天、医生决定疗程及未知安全性限定保留；片段间有重叠，末句跨原页续接，不据局部作完整医学结论。",
    47: "西语清洁前断电、步骤1-4、禁止喷洒及倾倒液体对应；仅文字转写，非照片理解。",
}
EXCLUDED = {
    1: "英文封面，仅保留原件及文档识别信息，不作为问答正文证据。",
    3: "目录/箱内清单混合页，不作为正文检索；清单如需使用，应另建带来源的结构化片段。",
    35: "西语封面，仅保留原件及文档识别信息，不作为问答正文证据。",
    37: "西语目录/箱内清单页，印刷页码不是PDF物理页码；不作正文检索。",
    70: "空白页，仅含印刷页码及自动来源文字，排除噪声证据。",
    71: "空白页，仅含印刷页码及自动来源文字，排除噪声证据。",
    72: "封底版本/厂家联系信息，保留原件与来源元数据，不作临床或操作正文证据。",
}
PREFIX = "AI技术验收（Codex，非人工/医学审批）："


def chunk_digest(chunks):
    return hashlib.sha256(json.dumps(chunks, ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()


def build_plan(evidence, old_ledger):
    if evidence["fileId"] != 7 or evidence["sourceSha256"] != SOURCE:
        raise ValueError("Only the visually inspected file #7 with matching SHA is allowed")
    if evidence["pageCount"] != 72 or evidence["chunkCount"] != 144 or evidence["published"]:
        raise ValueError("Unexpected source/index/publication state")
    risk = {p["physicalPage"] for p in evidence["pages"] if p["auditStatus"] == "needs_review"}
    old = {r["page"]: r for r in old_ledger["reviews"]}
    if risk != set(old) or old_ledger["sourceSha256"] != SOURCE:
        raise ValueError("Risk-page evidence drift")
    approvals = REPAIRED | TEXT_ONLY
    if set(approvals) & set(EXCLUDED) or not (set(approvals) | set(EXCLUDED)) <= risk:
        raise ValueError("Invalid decision coverage")
    plan = []
    for page in evidence["pages"]:
        n = page["physicalPage"]
        if n not in risk:
            continue
        if n in approvals:
            decision, note = "approved", approvals[n]
        elif n in EXCLUDED:
            decision, note = "excluded", EXCLUDED[n]
        else:
            decision, note = "needs_review", old[n]["note"]
            if n in (26, 27, 60, 61):
                note = "根号公式和表格已入库，但相邻原页系数/频段存在差异，尚未消除；不确认设备间距计算或摆放建议，保留待修复/澄清。"
        if decision == "approved" and not page["indexedChunks"]:
            raise ValueError("Cannot approve a page without index evidence")
        plan.append({"page": n, "decision": decision, "note": PREFIX + note,
                     "indexDigest": chunk_digest(page["indexedChunks"])})
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, default=Path("evaluation/accelstim_page_review.json"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    evidence = json.loads((args.folder / "page_evidence.json").read_text(encoding="utf-8"))
    plan = build_plan(evidence, json.loads(args.ledger.read_text(encoding="utf-8")))
    result = {"fileId": 7, "reviewType": "AI technical verification, not human or clinical approval",
              "sourceSha256": SOURCE, "counts": dict(Counter(p["decision"] for p in plan)),
              "unchangedAutoOk": 21, "plan": plan, "applied": False}
    (args.folder / "technical_review_plan.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.apply:
        from sqlalchemy import select, text
        from app.audit import audit
        from app.db import MarkdownFile, PdfPageAudit, db_session
        from app.pdf_quality import _sha, _source_path, audit_summary
        from scripts.stage_accelstim_repair import original_snapshot

        original_before = original_snapshot(2)
        with db_session() as db:
            file = db.scalar(select(MarkdownFile).where(MarkdownFile.id == 7).with_for_update())
            if not file or file.status != 2 or file.is_published or file.review_status != "draft" or file.reviewer_id:
                raise RuntimeError("Candidate is no longer an unsigned processed draft")
            if _sha(_source_path(file)) != SOURCE or file.content_sha256 != SOURCE:
                raise RuntimeError("Source changed since visual inspection")
            pages = db.scalars(select(PdfPageAudit).where(PdfPageAudit.file_id == 7)
                              .order_by(PdfPageAudit.page_number).with_for_update()).all()
            if len(pages) != 72:
                raise RuntimeError("Page set changed")
            live = db.execute(text("SELECT id,content,metadata FROM t_vector_store WHERE "
                "metadata->>'mdStorageId'='7' ORDER BY (metadata->>'pdfPage')::int,"
                "(metadata->>'chunkIndex')::int NULLS LAST,id")).mappings().all()
            if len(live) != 144:
                raise RuntimeError("Index changed")
            by_page = {p.page_number: p for p in pages}
            for item in plan:
                row = by_page[item["page"]]
                chunks = [{"id": str(c["id"]), "content": c["content"], "metadata": c["metadata"]}
                          for c in live if str(c["metadata"].get("pdfPage")) == str(item["page"])]
                if row.source_sha256 != SOURCE or chunk_digest(chunks) != item["indexDigest"]:
                    raise RuntimeError("Reviewed index is stale")
                if row.reviewer_id is not None or row.status != "needs_review" or row.note:
                    raise RuntimeError("Do not overwrite an existing review; export and inspect again")
            backup = {"fileId": 7, "pdfQaRevision": file.pdf_qa_revision, "remark": file.remark,
                      "pages": [{"page": p.page_number, "status": p.status, "note": p.note,
                          "reviewerId": p.reviewer_id, "reviewedAt": p.reviewed_at} for p in pages]}
            backup_path = args.folder / "before_technical_review.json"
            # Never overwrite the recovery evidence of a previous application.
            with backup_path.open("x", encoding="utf-8") as handle:
                json.dump(backup, handle, ensure_ascii=False, indent=2, default=str)
            actor = SimpleNamespace(id=None, username="Codex-AI-technical-review")
            for item in plan:
                row = by_page[item["page"]]
                row.status, row.note = item["decision"], item["note"]
                row.reviewed_at = datetime.now()
                # NULL reviewer_id is deliberate: no human signed this record.
                audit(db, "pdf_page_ai_review", 7,
                      f"page={row.page_number}; decision={row.status}; {row.note}", actor=actor)
            file.pdf_qa_revision = (file.pdf_qa_revision or 0) + 1
            file.update_time = datetime.now()
            file.remark = (file.remark or "") + "\nAI技术验收：24页确认文字索引、7页排除、20页待修复；非人工/医学审批，未发布。"
            db.flush()
            result["summary"] = audit_summary(db, file)
            if result["summary"]["ready"] or result["summary"]["needsReview"] != 20:
                raise RuntimeError("Unresolved pages must continue to block publication")
            if original_snapshot(2) != original_before:
                raise RuntimeError("Original version drifted")
        result["applied"] = True
        result["originalVersionUnchanged"] = original_snapshot(2) == original_before
        result["humanReviewerAssigned"] = False
        result["published"] = False
        (args.folder / "technical_review_receipt.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = ["# 文件 #7 v2 AI 技术验收", "",
        "范围：51个风险物理页的原页画面、提取文本与实际索引；21个auto_ok页本轮未逐页视觉签核，状态不变。",
        "核对记录：24页确认文字索引可用、7页排除、20页仍待修复。不是临床验证、人工签署或发布审批。",
        "原页中的图示未提取、截图噪声、符号对应、参考文献混排及公式差异，不会因为用户授权而直接判定通过。",
        "第31页已用250dpi原页复核：原表写的是256-356天，与索引一致；先前低清误读已纠正，没有改动原表数值。",
        f"数据库已应用：{result['applied']}。旧版#2及原PDF不修改；整份文档仍为草稿、未发布。", "",
        "| PDF物理页 | 技术结论 | 核对记录 |", "|---|---|---|"]
    for item in plan:
        lines.append(f"| {item['page']} | {item['decision']} | {item['note']} |")
    (args.folder / "technical_review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("plan", "summary")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
