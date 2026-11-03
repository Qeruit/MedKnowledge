"""Read-only release preflight; never approves pages, publishes or grants access."""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT, settings
from app.db import MarkdownFile, PdfPageAudit
from app.pdf_quality import audit_summary, require_pdf_audit_ready
from app.release_quality import require_passing_release_eval


def gate(check):
    try:
        check()
        return {"status": "PASS"}
    except HTTPException as exc:
        return {"status": "BLOCKED", "reason": exc.detail}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file-id", type=int, default=2)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "reports/demo")
    args = parser.parse_args()
    engine = create_engine(settings.database_url)
    with engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        with Session(bind=connection) as db:
            row = db.get(MarkdownFile, args.file_id)
            if row is None or not row.original_file_name.lower().endswith(".pdf"):
                raise SystemExit("An existing indexed PDF file is required")
            summary = audit_summary(db, row)
            pages = db.scalars(select(PdfPageAudit).where(PdfPageAudit.file_id == row.id)
                               .order_by(PdfPageAudit.page_number)).all()
            current = db.scalar(select(MarkdownFile).where(
                MarkdownFile.document_key == row.document_key, MarkdownFile.is_published.is_(True)))
            baseline = current if current and current.id != row.id else None
            report = {"createdAt": datetime.now().isoformat(), "fileId": row.id,
                "fileName": row.original_file_name, "sourceUrl": row.source_url,
                "sourceSha256": summary["sourceSha256"], "reviewStatus": row.review_status,
                "published": row.is_published, "pageCount": summary["pageCount"],
                "pageStates": dict(Counter(p.status for p in pages)),
                "pdfGate": gate(lambda: require_pdf_audit_ready(db, row)),
                "qualityGate": gate(lambda: require_passing_release_eval(db, row, baseline)),
                "independentReviewerRequired": True,
                "pages": summary["pages"],
                "scope": "read-only preflight, not human sign-off or release acceptance"}
        connection.rollback()
    engine.dispose()
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / f"second-document-{args.file_id}-readiness"
    target.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 第二份资料发布前核对清单", "", f"资料：{report['fileName']}（ID {row.id}）",
        f"时间：{report['createdAt']}", f"状态：{report['reviewStatus']}；已发布：{report['published']}",
        f"物理页数：{report['pageCount']}；页状态：{report['pageStates']}",
        f"PDF 门禁：{report['pdfGate']}", f"质量门禁：{report['qualityGate']}", "",
        "本工具使用数据库只读事务，不更改页面结论、审批、授权、索引或题库。", "",
        "## 下一步操作", "",
        "1. 在知识库管理 → 对应文件 → PDF 逐页验收，逐页比较原页预览、抽取文本和索引片段。",
        "2. 对准确完整的页附具体理由确认；图标、表格或关键步骤缺失的页修正后重传，或明确排除。不要批量批准。",
        "3. 页结论改变后重新运行当前版本的质量评测；旧报告可能失效。",
        "4. 由非上传者的真实管理员完成内容审核，再由管理员确认目标查询者的授权范围。",
        f"5. 第二份资料发布并授权后，运行 demo/demo.ps1 -Action Verify -SecondFileId {args.file_id}，另在网页验证切换后的来源和历史。", "",
        "跨资料集成测试使用合成资料和模拟模型，不等于本手册的真实模型问答验收。", "",
        "## 待核对页（物理页码）", "", "| 页码 | 风险标记 | 字符数 | 索引片段数 |", "|---|---|---:|---:|"]
    for page in report["pages"]:
        if page["status"] == "needs_review":
            lines.append(f"| {page['pageNumber']} | {', '.join(page['flags'])} | {page['extractedChars']} | {page['indexedChunks']} |")
    target.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"REPORT={target.with_suffix('.md')}")
    print(f"PDF_GATE={report['pdfGate']['status']}; QUALITY_GATE={report['qualityGate']['status']}")


if __name__ == "__main__":
    main()
