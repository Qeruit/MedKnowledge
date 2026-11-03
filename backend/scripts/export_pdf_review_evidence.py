"""Export page text and index evidence in a DB-enforced read-only transaction."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from pypdf import PdfReader
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.db import MarkdownFile, PdfPageAudit
from app.pdf_quality import _source_path


def compact(value):
    return "".join(value.split())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        with Session(bind=conn) as db:
            file = db.get(MarkdownFile, args.file_id)
            if file is None:
                raise SystemExit("File not found")
            source = _source_path(file)
            sha = hashlib.sha256(source.read_bytes()).hexdigest()
            if sha != file.content_sha256:
                raise SystemExit("Source hash mismatch")
            pdf = PdfReader(source)
            audits = {a.page_number: a for a in db.scalars(select(PdfPageAudit).where(
                PdfPageAudit.file_id == file.id)).all()}
            chunks = db.execute(text("""SELECT id,content,metadata FROM t_vector_store
                WHERE metadata->>'mdStorageId'=:id ORDER BY (metadata->>'pdfPage')::int,
                (metadata->>'chunkIndex')::int NULLS LAST,id"""), {"id": str(file.id)}).mappings().all()
            pages = []
            for number, page in enumerate(pdf.pages, 1):
                extracted = page.extract_text() or ""
                audit = audits.get(number)
                indexed = [{"id": str(c["id"]), "content": c["content"], "metadata": c["metadata"]}
                           for c in chunks if str(c["metadata"].get("pdfPage")) == str(number)]
                joined = compact("".join(c["content"] for c in indexed))
                # Substring check is a provenance aid, not layout/semantic QA.
                lines = [line.strip() for line in extracted.splitlines() if line.strip()]
                missing = [line for line in lines if len(compact(line)) >= 4 and compact(line) not in joined]
                pages.append({"physicalPage": number, "label": page.get("/PageLabels"),
                    "auditStatus": audit.status if audit else None,
                    "flags": audit.flags if audit else [], "auditText": audit.extracted_text if audit else None,
                    "sourceText": extracted, "indexedChunks": indexed,
                    "nontrivialSourceLines": len([x for x in lines if len(compact(x)) >= 4]),
                    "missingSourceLines": missing})
            evidence = {"exportedAt": datetime.now(timezone.utc).isoformat(), "fileId": file.id,
                "fileName": file.original_file_name, "sourceSha256": sha, "sourceUrl": file.source_url,
                "reviewStatus": file.review_status, "published": file.is_published,
                "pageCount": len(pdf.pages), "chunkCount": len(chunks), "pages": pages}
        conn.rollback()
    engine.dispose()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "page_evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    for item in pages:
        (args.output / f"page-{item['physicalPage']:02d}.txt").write_text(
            f"PHYSICAL PAGE {item['physicalPage']}\nFLAGS: {item['flags']}\n\nSOURCE TEXT\n{item['sourceText']}"
            + "\n\nINDEXED CHUNKS\n" + "\n\n".join(c["content"] for c in item["indexedChunks"])
            + f"\n\nMISSING SOURCE LINES (mechanical check only):\n{item['missingSourceLines']}\n", encoding="utf-8")
    print(f"Exported {len(pages)} pages and {len(chunks)} chunks; source DB was read-only.")


if __name__ == "__main__":
    main()
