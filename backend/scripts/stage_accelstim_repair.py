"""Create an AI-repaired draft via normal upload/version APIs; NEVER approve it."""
import argparse
import hashlib
import json
from pathlib import Path
import time

import httpx
from sqlalchemy import select, text

from app.accelstim_tables import SOURCE_SHA256, REVISION, REPAIRED_PAGES, PROFILE_SHA256
from app.config import PROJECT_ROOT
from app.db import MarkdownFile, PdfPageAudit, db_session
from app.documents import read_documents
from app.pdf_quality import _source_path

LABEL = "AI table/formula repair v1 - pending human confirmation"


def original_snapshot(file_id):
    with db_session() as db:
        file = db.get(MarkdownFile, file_id)
        vectors = db.execute(text("SELECT id,content,metadata FROM t_vector_store WHERE metadata->>'mdStorageId'=:id ORDER BY id"),
                             {"id": str(file_id)}).mappings().all()
        pages = db.scalars(select(PdfPageAudit).where(PdfPageAudit.file_id == file_id)
                          .order_by(PdfPageAudit.page_number)).all()
        data = {"state": [file.status, file.review_status, file.is_published, file.pdf_qa_revision,
                          file.reviewer_id, file.reviewed_at],
                "vectors": [dict(v) for v in vectors],
                "pages": [[p.page_number,p.status,p.note,p.reviewer_id,p.reviewed_at] for p in pages]}
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-id", type=int, default=2)
    parser.add_argument("--base-url", default="http://web")
    parser.add_argument("--password-file", type=Path, default=Path("/app/.credentials/admin-bootstrap.json"))
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "reports/demo/accelstim_repair")
    args = parser.parse_args()
    with db_session() as db:
        base = db.get(MarkdownFile, args.base_id)
        if base is None or base.content_sha256 != SOURCE_SHA256 or base.document_key is None:
            raise RuntimeError("Expected registered AccelStim source required")
        source = _source_path(base)
        if hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE_SHA256:
            raise RuntimeError("Source hash mismatch")
        existing = db.scalars(select(MarkdownFile).where(MarkdownFile.document_key == base.document_key,
                              MarkdownFile.revision_label == LABEL)).all()
        if len(existing) > 1:
            raise RuntimeError("Ambiguous repair drafts")
        candidate_id = existing[0].id if existing else None
        refresh = bool(existing and PROFILE_SHA256 not in (existing[0].index_config or {}).get("pdfRepairProfileHashes", []))
    before = original_snapshot(base.id)
    # Offline inspection before performing the upload, and portable artefacts.
    args.output.mkdir(parents=True, exist_ok=True)
    audits = []
    chunks = read_documents(source, base.original_file_name, page_audits=audits)
    preview = {"sourceSha256": SOURCE_SHA256,"revision": REVISION,
               "reviewMode": "AI assisted, not human sign-off", "pages": audits,
               "chunks": [{"text": c.text,"metadata": c.metadata} for c in chunks]}
    (args.output / "repaired_preview.json").write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8")
    for number in sorted(REPAIRED_PAGES):
        (args.output / f"repaired-page-{number:02d}.md").write_text(
            "\n\n---\n\n".join(c.text for c in chunks if c.metadata.get("pdfPage") == number), encoding="utf-8")
    credential = json.loads(args.password_file.read_text(encoding="utf-8"))
    logged_in = False
    with httpx.Client(base_url=args.base_url, timeout=45, headers={"Origin": "http://127.0.0.1:8088"}) as client:
        def post(path, body=None, **kwargs):
            response = client.post("/api" + path, json=body, **kwargs)
            response.raise_for_status()
            value = response.json()
            if not value.get("success"):
                raise RuntimeError(f"API refused {path}: {value.get('message')}")
            return value.get("data")
        try:
            info = post("/auth/login", {"username": credential["username"], "password": credential["password"]})
            logged_in = True
            client.headers["X-CSRF-Token"] = info["csrfToken"]
            if info["id"] != base.owner_id:
                raise RuntimeError("Existing file owner must upload this candidate; no impersonation or reassignment")
            if candidate_id is None:
                with source.open("rb") as content:
                    data = post("/customer-service/md/upload", files={"file": (base.original_file_name, content, "application/pdf")})
                candidate_id = data["id"]
                post("/customer-service/md/update", {"id": candidate_id,
                    "remark": f"AI辅助表格/公式修复；{REVISION}。非人工审批。原版ID={base.id}；原文件未改；保留页门禁。"})
                post("/customer-service/md/versions/register", {"id": candidate_id,"baseId": base.id,
                    "revisionLabel": LABEL,"sourceUrl": base.source_url})
                print(f"Created AI repair draft fileId={candidate_id}; indexing queued; no approval.", flush=True)
            elif refresh:
                post("/customer-service/md/reindex", {"id": candidate_id})
                print(f"Refreshing editable AI repair draft fileId={candidate_id}; no approval.", flush=True)
            deadline = time.monotonic() + 600
            while True:
                with db_session() as db:
                    candidate = db.get(MarkdownFile, candidate_id)
                    if candidate.status == 2:
                        break
                    if candidate.status == 3:
                        raise RuntimeError("Indexing failed; inspect candidate processingError")
                if time.monotonic() > deadline:
                    raise TimeoutError("Indexing pending; no manual status changes performed")
                time.sleep(2)
            with db_session() as db:
                rows = db.execute(text("SELECT content,metadata FROM t_vector_store WHERE metadata->>'mdStorageId'=:id"),
                                  {"id": str(candidate_id)}).mappings().all()
                fixed_pages = {int(r["metadata"]["pdfPage"]) for r in rows if r["metadata"].get("pdfRepairRevision") == REVISION}
                states = db.scalars(select(PdfPageAudit).where(PdfPageAudit.file_id == candidate_id)).all()
                p66 = "\n".join(r["content"] for r in rows if r["metadata"].get("pdfPage") == 66)
                row_checks = []
                for number in sorted(REPAIRED_PAGES):
                    actual = "\n".join(r["content"] for r in rows if r["metadata"].get("pdfPage") == number)
                    expected = {line for c in chunks if c.metadata.get("pdfPage") == number
                                for line in c.text.splitlines() if line.startswith("| ")
                                and not line.startswith("| ---")}
                    row_checks.append({"page": number,"tableLines": len(expected),
                                       "missingLines": sorted(line for line in expected if line not in actual)})
                checks = {
                    "all_22_repaired_pages_indexed": fixed_pages == REPAIRED_PAGES,
                    "femur_row_correct": "| Femur | 13 | 12 | 1 | 92% |" in p66,
                    "tibia_row_correct": "| Tibia/Tibia-Fibula/Fibula | 28 | 26 | 2 | 93% |" in p66,
                    "formula_present": any("d=1.2*sqrt(P)" in r["content"] for r in rows if r["metadata"].get("pdfPage") == 27),
                    "conflict_metadata_retained": all(r["metadata"].get("pdfRepairSourceConflict") is True for r in rows if r["metadata"].get("pdfPage") in (26,27,60,61)),
                    "repaired_pages_not_auto_approved": all(p.status == "needs_review" and p.reviewer_id is None for p in states if p.page_number in REPAIRED_PAGES),
                    "unpublished_draft": candidate.review_status == "draft" and not candidate.is_published and candidate.reviewer_id is None,
                    "original_unchanged": before == original_snapshot(base.id),
                    "all_structured_table_lines_preserved": all(not c["missingLines"] for c in row_checks),
                    "repair_profile_hash_recorded": all(r["metadata"].get("pdfRepairProfileSha256") == PROFILE_SHA256
                        for r in rows if r["metadata"].get("pdfRepairRevision")),
                }
                report = {"candidateId": candidate_id,"version": candidate.version_number,
                          "baseId": base.id,"sourceSha256": SOURCE_SHA256,"revision": REVISION,
                          "status": candidate.status,"reviewStatus": candidate.review_status,
                          "published": candidate.is_published,"chunks": len(rows),
                          "repairedPages": sorted(fixed_pages),"indexConfig": candidate.index_config,
                          "tableLineChecks": row_checks,"checks": checks}
            (args.output / "staging_result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
            print(json.dumps(report,ensure_ascii=False),flush=True)
            if not all(checks.values()):
                raise RuntimeError("Repair candidate verification failed; no release attempted")
        finally:
            if logged_in:
                client.post("/api/auth/logout")


if __name__ == "__main__":
    main()
