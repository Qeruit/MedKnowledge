"""Print ranked public knowledge headings for a supplied diagnostic question."""
import asyncio
import sys

from sqlalchemy import text

from app.db import db_session
from app.llm import embed_texts, vector_literal
from scripts.auth_helpers import evaluation_principal


async def main(question):
    principal = evaluation_principal()
    vector = (await embed_texts([question]))[0]
    with db_session() as db:
        rows = db.execute(text("""
            SELECT v.content, v.embedding <=> CAST(:embedding AS vector) AS distance
            FROM t_vector_store v JOIN t_ai_customer_service_md_storage f
              ON v.metadata->>'mdStorageId'=CAST(f.id AS text)
            WHERE f.status=2 AND f.is_published
              AND (:admin OR f.owner_id=:uid OR EXISTS
                (SELECT 1 FROM t_file_grant g WHERE g.file_id=f.id AND g.user_id=:uid))
            ORDER BY distance LIMIT 20
        """), {"embedding": vector_literal(vector), "uid": principal["user_id"],
                 "admin": principal["role"] == "admin"}).all()
    for row in rows:
        print(f"{row.distance:.3f} {row.content.splitlines()[0]}")


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:])))
