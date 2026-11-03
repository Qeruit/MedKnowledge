"""Inspect vector ranking for one file without changing its publication state."""
import argparse
import asyncio

from sqlalchemy import text

from app.db import db_session
from app.knowledge import CONTEXT_CANDIDATES, ENGLISH_PDF_HINTS, _search_terms, select_relevant_context
from app.llm import embed_texts, vector_literal


async def main(file_id: int, question: str):
    vector = (await embed_texts([question]))[0]
    with db_session() as db:
        rows = db.execute(text("""
            SELECT content, metadata, embedding <=> CAST(:vector AS vector) AS distance
            FROM t_vector_store WHERE metadata->>'mdStorageId'=:file
            ORDER BY embedding <=> CAST(:vector AS vector) LIMIT :limit
        """), {"file": str(file_id), "vector": vector_literal(vector),
                "limit": CONTEXT_CANDIDATES}).all()
    selected = select_relevant_context(question, rows)
    hints = [words for phrase, words in ENGLISH_PDF_HINTS if phrase in question]
    for rank, row in enumerate(rows, 1):
        _, _, body = row.content.partition("\n")
        terms = _search_terms(body)
        matched = ["/".join(sorted(words)) for words in hints if terms & words]
        print(f"{rank:2} distance={row.distance:.3f} pdfPage={row.metadata.get('pdfPage')} "
              f"selected={row.content in selected} hints={matched} "
              f"text={row.content[:140].replace(chr(10), ' ')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file_id", type=int)
    parser.add_argument("question")
    args = parser.parse_args()
    asyncio.run(main(args.file_id, args.question))
