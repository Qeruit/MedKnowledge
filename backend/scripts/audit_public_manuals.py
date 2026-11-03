"""Create page audits for already indexed public-manual drafts, without re-embedding."""

import json
from pathlib import Path

from sqlalchemy import select

from app.config import PROJECT_ROOT
from app.db import MarkdownFile, PdfPageAudit, db_session
from app.pdf_quality import audit_summary, create_page_audit


def main():
    manifest = json.loads((PROJECT_ROOT / "data" / "public_manuals" / "device_manuals.json").read_text(encoding="utf-8"))
    for entry in manifest:
        with db_session() as db:
            rows = db.scalars(select(MarkdownFile).where(MarkdownFile.original_file_name == entry["file"])).all()
            if len(rows) != 1 or rows[0].content_sha256 != entry["sha256"]:
                raise RuntimeError(f"资料记录或 SHA-256 与清单不符：{entry['file']}")
            row = rows[0]
            if not db.scalar(select(PdfPageAudit.file_id).where(PdfPageAudit.file_id == row.id).limit(1)):
                create_page_audit(db, row)
            result = audit_summary(db, row)
            print(json.dumps({"fileId": row.id, "fileName": row.original_file_name,
                              "pdfPages": result["pageCount"], "audited": result["auditedPages"],
                              "needsReview": result["needsReview"], "excluded": result["excluded"],
                              "ready": result["ready"],
                              "sampleRiskPages": [p["pageNumber"] for p in result["pages"]
                                                  if p["status"] == "needs_review"][:20]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
