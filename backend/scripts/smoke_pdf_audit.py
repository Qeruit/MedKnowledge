"""Staging smoke test for page comparison and release gate; no knowledge mutations."""

import asyncio

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import knowledge
from app.db import MarkdownFile, PdfPageAudit, User, db_session
from app.main import app
from app.pdf_quality import require_pdf_audit_ready
from scripts.auth_helpers import login_client


def main():
    client = TestClient(app)
    login_client(client)
    try:
        for file_id, expected_pages in ((2, 72), (3, 363), (4, 402)):
            response = client.post("/customer-service/md/pdf/pages", json={"id": file_id})
            assert response.status_code == 200, response.text
            report = response.json()["data"]
            assert report["pageCount"] == report["auditedPages"] == expected_pages
            assert report["needsReview"] > 0 and not report["ready"]
            with db_session() as db:
                try:
                    require_pdf_audit_ready(db, db.get(MarkdownFile, file_id))
                except HTTPException as exc:
                    assert exc.status_code == 409
                else:
                    raise AssertionError("Unreviewed PDF must not pass publication gate")
            print(f"#{file_id}: {expected_pages} pages; {report['needsReview']} require review; release blocked")
        detail = client.post("/customer-service/md/pdf/page", json={"id": 3, "pageNumber": 83})
        assert detail.status_code == 200, detail.text
        page = detail.json()["data"]
        assert page["status"] == "needs_review" and "embedded_image_unread" in page["flags"]
        assert page["indexedChunks"] and "暂停" in page["extractedText"]
        preview = client.get("/customer-service/md/pdf/preview/3/83")
        assert preview.status_code == 200, preview.text[:200]
        assert preview.headers["content-type"] == "image/png" and preview.content.startswith(b"\x89PNG")
        print("MiniMed PDF 83: extracted text + indexed chunks + protected page preview OK")
        original_embed = knowledge.embed_texts
        async def local_vector(questions):
            return [[0.1] * 1536 for _ in questions]
        knowledge.embed_texts = local_vector
        try:
            with db_session() as db:
                principal = db.scalar(select(User).where(User.username == "admin"))
            trace = {}
            asyncio.run(knowledge.find_context("MiniMed 780G", user_id=principal.id, role="admin",
                                               trace=trace, evaluation_file_id=3))
            candidate_pages = {item["metadata"]["pdfPage"] for part in trace["candidates"]
                               for item in part["rows"]}
            assert candidate_pages and 83 not in candidate_pages and 84 not in candidate_pages
            with db_session() as db:
                assert all(db.get(PdfPageAudit, (3, number)).status in ("auto_ok", "approved")
                           for number in candidate_pages)
            print(f"Candidate retrieval SQL filtered risk pages; {len(candidate_pages)} accepted pages sampled")
        finally:
            knowledge.embed_texts = original_embed
    finally:
        client.close()


if __name__ == "__main__":
    main()
