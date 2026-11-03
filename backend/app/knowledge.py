"""Markdown indexing, vector search and file lifecycle for the customer-service page."""

from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import text
from fastapi import HTTPException

from app.config import settings
from app.db import MarkdownFile, PdfPageAudit, db_session
from app.llm import embed_texts, embed_texts_sync, vector_literal
from app.splitter import split_documents
from app.pdf_rerank import rank_pdf_passages, select_pdf_passages
from app.documents import DocumentReadError, read_documents
from app.access import readable_scope
from sqlalchemy import delete, select


logger = logging.getLogger(__name__)
CONTEXT_CANDIDATES = 16
CONTEXT_LIMIT = 5
LEXICAL_CANDIDATES = 16
# pgvector's cosine operator returns a distance: smaller means more relevant.
# Calibrated against the current embedding model and the known in/out-of-domain questions.
MAX_CONTEXT_DISTANCE = 0.55
QUESTION_FILLERS = {"什么", "怎么", "如何", "是否", "请问", "一下", "哪些", "可以",
                    "全部", "所有", "系统"}
PRODUCT_IDENTIFIER = re.compile(r"[a-z][a-z0-9]*(?:[-–][a-z0-9]+)+", re.IGNORECASE)
# A semantic nearest neighbour is not necessarily evidence. For Chinese FAQ
# cards, require at least one question-specific topic bigram in the passage.
# This gate deliberately does not apply to English PDF pages, where literal
# Chinese overlap is not a reliable relevance signal.
QUESTION_FRAMING = re.compile(
    r"请问|请帮我|告诉我|能不能|有没有|是不是|是什么|怎么样|怎么办|怎么|如何|是否|"
    r"多少|多久|哪些|可以|能够|一下|这个|那个|上述|前述|其中|那么|那它|它的|"
    r"的|是|和|与|或|及|吗|呢|吧|呀|？|\?|，|,|。"
)
# Sparse cross-language hints are only a fallback for English PDF pages queried
# in Chinese. They supplement, rather than replace, multilingual embeddings.
ENGLISH_PDF_HINTS = (
    ("存储", {"storage"}), ("储存", {"storage"}),
    ("运输", {"transport"}), ("温度", {"temperature"}),
    ("治疗", {"treatment", "therapeutic"}), ("疗程", {"treatment", "therapeutic"}),
    ("每天", {"daily", "day"}), ("每日", {"daily", "day"}),
    ("小时", {"hours", "hour"}), ("多少天", {"days", "day"}),
    ("充电", {"charging", "charge"}), ("电池", {"battery"}),
    ("警告", {"warnings", "warning"}),
)


def _matches_explicit_product(question: str, row) -> bool:
    """Do not pass evidence for a different hyphenated device model to the LLM."""
    requested = PRODUCT_IDENTIFIER.search(question)
    if not requested:
        return True
    identifier = re.sub(r"[^a-z0-9]", "", requested.group().lower())
    metadata = getattr(row, "metadata", None) or {}
    evidence = f"{metadata.get('originalFileName', '')} {row.content[:300]}"
    return identifier in re.sub(r"[^a-z0-9]", "", evidence.lower())


def split_markdown(source: str) -> list[str]:
    """Compatibility text view of the structure-aware documents."""
    return [chunk.text for chunk in split_documents(source)]


def _metadata(file_id: int, original_name: str, source_url: str | None, chunk: str):
    return {
        "mdStorageId": file_id,
        "originalFileName": original_name,
        "sourceUrl": source_url,
        "contentSha256": hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
    }


def index_markdown(file_id: int, job: dict):
    """Independent Worker computes outside transactions, then commits under a fence."""
    from app.task_queue import fence, succeed
    with db_session() as db:
        file = db.get(MarkdownFile, file_id)
        if file is None:
            raise FileNotFoundError("资料记录不存在")
        file_path = Path(file.file_path)
        original_name = file.original_file_name

    try:
        page_audits = []
        documents = read_documents(file_path, original_name, page_audits=page_audits)
        if not documents:
            raise ValueError("Markdown 文件没有可向量化的正文")
        embedded = []
        for offset in range(0, len(documents), 10):
            # Stop between batches if execution time/ownership has expired.
            with db_session() as db:
                fence(db, job)
            batch = documents[offset:offset + 10]
            embedded.extend(zip(batch, embed_texts_sync([doc.text for doc in batch]), strict=True))
        with db_session() as db:
            fence(db, job)
            file = db.get(MarkdownFile, file_id)
            source_url = file.source_url
            if page_audits:
                db.execute(delete(PdfPageAudit).where(PdfPageAudit.file_id == file_id))
                source_sha = file.content_sha256 or hashlib.sha256(file_path.read_bytes()).hexdigest()
                for item in page_audits:
                    db.add(PdfPageAudit(file_id=file_id, page_number=item["pageNumber"],
                                        page_label=item["pageLabel"], extracted_text=item["extractedText"],
                                        flags=item["flags"], status=item["status"], source_sha256=source_sha))
                file.pdf_qa_revision = (file.pdf_qa_revision or 0) + 1
            # A retry should replace only this file's vectors, never another file's identical text.
            db.execute(text("DELETE FROM t_vector_store WHERE metadata ->> 'mdStorageId' = :id"), {"id": str(file_id)})
            for chunk, vector in embedded:
                stable_id = uuid5(NAMESPACE_URL, f"{file_id}:{chunk.metadata['chunkIndex']}:{hashlib.sha256(chunk.text.encode()).hexdigest()}")
                db.execute(text("""
                    INSERT INTO t_vector_store (id, content, metadata, embedding)
                    VALUES (:id, :content, CAST(:metadata AS json), CAST(:embedding AS vector))
                    ON CONFLICT (id) DO UPDATE SET
                      content = EXCLUDED.content,
                      metadata = EXCLUDED.metadata,
                      embedding = EXCLUDED.embedding
                """), {
                    "id": stable_id, "content": chunk.text,
                    "metadata": json.dumps({**_metadata(file_id, original_name, chunk.metadata.get('sourceUrl') or source_url, chunk.text),
                                            **chunk.metadata}, ensure_ascii=False),
                    "embedding": vector_literal(vector),
                })
            file.status = 2
            file.processing_error = None
            file.embedding_model = settings.embedding_model
            file.index_config = {
                "embeddingModel": settings.embedding_model,
                "dimensions": settings.embedding_dimensions,
                "splitterEnabled": settings.rag_splitter_enabled,
                "chunkTokens": settings.rag_chunk_tokens,
                "overlapTokens": settings.rag_overlap_tokens,
                "tokenEncoding": settings.rag_token_encoding,
                  "pdfRepairProfiles": sorted({doc.metadata["pdfRepairRevision"] for doc in documents
                                               if doc.metadata.get("pdfRepairRevision")}),
                  "pdfRepairProfileHashes": sorted({doc.metadata["pdfRepairProfileSha256"] for doc in documents
                                                    if doc.metadata.get("pdfRepairProfileSha256")}),
            }
            file.processing_warnings = list(dict.fromkeys(warning for doc in documents
                                                          for warning in doc.metadata.get("readerWarnings", [])))
            file.update_time = datetime.now()
            succeed(db, job)
    except Exception as exc:
        # Retry classification and status transitions belong to the durable queue.
        raise


def _search_terms(value: str) -> set[str]:
    """Small lexical signal to complement embeddings for short Chinese FAQ cards."""
    value = PRODUCT_IDENTIFIER.sub(" ", value.lower())
    terms = set()
    for token in re.findall(r"[\u4e00-\u9fff]+|[a-z][a-z0-9-]{2,}|\d+", value):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            terms.update(token[i:i + 2] for i in range(len(token) - 1))
        else:
            terms.add(token)
    terms -= QUESTION_FILLERS
    # In this device corpus, a question about treatment duration must not be
    # dominated by the generic word "多久" in a battery-charging card.
    if "治疗" in value or "疗程" in value:
        terms.discard("多久")
        terms.update({"治疗", "疗程"})
        if "多久" in value or "多少天" in value:
            terms.update({"佩戴", "时间", "时长"})
        if "每天" in value:
            terms.add("每日")
    return terms


def _question_topic_terms(question: str) -> set[str]:
    """Drop device identifiers and question framing before lexical evidence gating."""
    core = QUESTION_FRAMING.sub(" ", PRODUCT_IDENTIFIER.sub(" ", question.lower()))
    terms = set()
    for group in re.findall(r"[\u4e00-\u9fff]+", core):
        terms.update(group[i:i + 2] for i in range(len(group) - 1))
    return terms


def _supports_question_topic(question: str, content: str, metadata: dict | None = None) -> bool:
    if (metadata or {}).get("pdfPage"):
        return True
    topics = _question_topic_terms(question)
    return not topics or bool(topics & _search_terms(content))


def select_relevant_context(question: str, rows) -> list[str]:
    """Reject weak matches and cover distinct facts in multi-part questions."""
    pdf_rows = [row for row in rows if (getattr(row, "metadata", None) or {}).get("pdfPage")
                and row.content and float(row.distance) <= MAX_CONTEXT_DISTANCE]
    if pdf_rows and all((getattr(row, "metadata", None) or {}).get("pdfPage") for row in rows):
        return select_pdf_passages(question, pdf_rows, CONTEXT_LIMIT)
    query_terms = _search_terms(question)
    candidates = []
    pdf_contents = set()
    pdf_header_terms = []
    for row in rows:
        content, distance = row.content, float(row.distance)
        if not content or distance > MAX_CONTEXT_DISTANCE:
            continue
        if not _supports_question_topic(question, content, getattr(row, "metadata", None)):
            continue
        heading, _, body = content.partition("\n")
        # PDF page headers are generated from the file name and page number.
        # Counting them as evidence lets an unrelated page win on model tokens
        # repeated in every header (e.g. "MiniMed 780G").
        metadata = getattr(row, "metadata", None) or {}
        is_pdf = bool(metadata.get("pdfPage"))
        heading_terms = set() if is_pdf else _search_terms(heading)
        candidates.append((content, distance, heading_terms, _search_terms(body)))
        if is_pdf:
            pdf_contents.add(content)
            pdf_header_terms.append(_search_terms(heading))

    # Terms repeated in every generated PDF header identify the file, not the
    # answer page. Do not let them justify adding a second, irrelevant page.
    common_pdf_header_terms = set.intersection(*pdf_header_terms) if pdf_header_terms else set()

    def relevant_terms(content):
        return query_terms - common_pdf_header_terms if content in pdf_contents else query_terms

    # The PDF reader keeps the original English body. A Chinese query may share
    # only the device name with every page; in that case lexical coverage would
    # stop after one page even though the embedding shortlist contains the fact.
    # Keep the distance gate and use the nearest few pages as a multilingual
    # fallback. Chinese FAQ cards still use the more precise coverage selector.
    pdf_candidates = [row for row in rows if (getattr(row, "metadata", None) or {}).get("pdfPage")]
    if pdf_candidates and re.search(r"[\u4e00-\u9fff]", question):
        if candidates and max(len(query_terms & (heading | body))
                              for _, _, heading, body in candidates) <= 1:
            hints = [words for phrase, words in ENGLISH_PDF_HINTS if phrase in question]
            # A page can be semantically close because it mentions the device,
            # yet omit the requested fact. Prefer pages covering more distinct
            # concepts before using cosine distance to break ties. Stop once
            # all requested concepts are evidenced; adding loosely related
            # pages encourages contradictory "not stated" qualifications.
            if hints:
                remaining = set(range(len(hints)))
                selected_pdf = []
                available_pdf = list(candidates)
                while remaining and available_pdf and len(selected_pdf) < CONTEXT_LIMIT:
                    best = min(available_pdf, key=lambda item: (
                        -sum(bool(item[3] & hints[i]) for i in remaining), item[1]))
                    hits = {i for i in remaining if best[3] & hints[i]}
                    if not hits:
                        break
                    selected_pdf.append(best[0])
                    remaining -= hits
                    available_pdf.remove(best)
                if selected_pdf:
                    return selected_pdf
            return [content for content, *_ in sorted(candidates, key=lambda item: item[1])[:3]]

    selected = []
    covered = set()
    denominator = max(len(query_terms), 1)
    while candidates and len(selected) < CONTEXT_LIMIT:
        # After the first hit, prefer a document that adds a fact the selected
        # passages have not covered yet, even if its vector rank is lower.
        available = candidates if not selected else [
            candidate for candidate in candidates
            if (relevant_terms(candidate[0]) & (candidate[2] | candidate[3])) - covered
        ]
        if not available:
            break

        def score(candidate):
            _, distance, heading_terms, body_terms = candidate
            uncovered = relevant_terms(candidate[0]) - covered
            heading_hits = len(uncovered & heading_terms)
            body_hits = len(uncovered & body_terms)
            # Long PDF pages contain many incidental query bigrams. Keep the
            # semantic ranking dominant; FAQ cards still use stronger lexical
            # coverage because they are short, curated answer units.
            body_weight = 0.08 if candidate[0] in pdf_contents else 0.60
            return distance - 0.65 * heading_hits / denominator - body_weight * body_hits / denominator

        best = min(available, key=score)
        candidates.remove(best)
        content, _, heading_terms, body_terms = best
        new_terms = (relevant_terms(content) & (heading_terms | body_terms)) - covered
        if selected and not new_terms:
            break
        selected.append(content)
        covered.update(new_terms)
        # A highly specific FAQ heading is enough for a single-intent question.
        # Avoid adding neighboring status-card instructions that could be conflated.
        heading_hits = len(query_terms & heading_terms)
        if len(selected) == 1 and heading_hits >= 3 and heading_hits / denominator >= 0.45:
            break
    return selected


def _retrieval_questions(question: str) -> list[str]:
    """Search each explicit subquestion, carrying a named product into follow-ups."""
    parts = [part.strip() for part in re.split(r"[?？;；]+", question) if part.strip()]
    if len(parts) <= 1:
        return [question]
    product = PRODUCT_IDENTIFIER.search(question)
    if not product:
        return parts[:4]
    identifier = product.group()
    return [
        part if identifier.lower() in part.lower() else f"{identifier} {part}"
        for part in parts[:4]
    ]


def _lexical_phrases(question: str) -> list[str]:
    """Build bounded, indexable fragments; keep values parameterized in SQL."""
    without_model = PRODUCT_IDENTIFIER.sub(" ", question.lower())
    phrases = []
    for group in re.findall(r"[\u4e00-\u9fff]+", without_model):
        for segment in re.split(r"(?:什么|如何|怎么|多少|是否|可以|请问|以及|或者|和|的|是)", group):
            if len(segment) >= 3:
                phrases.extend(segment[i:i + 4] for i in range(max(1, len(segment) - 3)))
    phrases.extend(word for word in re.findall(r"[a-z][a-z0-9-]{2,}", without_model)
                   if word not in {"what", "how", "the", "and", "for", "does", "are", "can", "with"})
    for chinese, english in ENGLISH_PDF_HINTS:
        if chinese in question:
            phrases.extend(sorted(english))
    # Two-character Chinese fragments cannot use the trigram index efficiently.
    return list(dict.fromkeys(phrase for phrase in phrases if len(phrase) >= 3))[:16]


def _fuse_candidates(vector_rows, lexical_rows):
    """Reciprocal-rank fusion keeps both channels independent of raw score scales."""
    merged = {}
    for channel, rows in (("vector", vector_rows), ("lexical", lexical_rows)):
        for rank, row in enumerate(rows, 1):
            key = row.id
            if key not in merged:
                merged[key] = {"row": row, "score": 0.0, "channels": []}
            merged[key]["score"] += 1 / (60 + rank)
            merged[key]["channels"].append(channel)
    return [SimpleNamespace(id=key, content=item["row"].content,
                            metadata=item["row"].metadata, distance=item["row"].distance,
                            channels=item["channels"])
            for key, item in sorted(merged.items(), key=lambda pair: (-pair[1]["score"],
                                                                      float(pair[1]["row"].distance), str(pair[0])))]


async def find_context(question: str, *, user_id: int, role: str, trace: dict | None = None,
                       evaluation_file_id: int | None = None, file_id: int | None = None) -> list[dict]:
    # Opt-in local evaluation only: never persist document text in normal logs.
    if trace is not None:
        trace.update({"subquestions": [], "candidates": [], "selected": []})
    if role not in {"viewer", "editor", "admin"} or user_id <= 0:
        raise ValueError("检索必须包含有效用户")
    if evaluation_file_id is not None and file_id is not None:
        raise ValueError("公开问答范围和内部评测范围不能混用")
    with db_session() as db:
        if evaluation_file_id is not None:
            # Internal release evaluation only. Never accept this parameter from a chat request.
            if not db.scalar(select(MarkdownFile.id).where(MarkdownFile.id == evaluation_file_id,
                                                       MarkdownFile.status == 2).limit(1)):
                return []
        elif file_id is not None:
            if not db.scalar(select(MarkdownFile.id).where(
                    MarkdownFile.id == file_id, readable_scope(SimpleNamespace(id=user_id, role=role)),
                    MarkdownFile.status == 2).limit(1)):
                raise HTTPException(404, "资料不存在、未发布或无查询权限，请刷新可查询资料")
        elif not db.scalar(select(MarkdownFile.id).where(
            readable_scope(SimpleNamespace(id=user_id, role=role)), MarkdownFile.status == 2).limit(1)):
            return []
    questions = _retrieval_questions(question)
    if trace is not None:
        trace["subquestions"] = questions
    vectors = await embed_texts(questions)
    contexts = []
    seen = set()
    with db_session() as db:
        # A multi-part question runs several vector queries. READ COMMITTED could
        # see v1 for the first part and v2 after an approval commits between parts.
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        for part, vector in zip(questions, vectors, strict=True):
            phrases = _lexical_phrases(part)
            scope_clause = ("f.id=:evaluation_file_id" if evaluation_file_id is not None else """
                f.is_published AND (f.document_key IS NULL OR f.review_status='approved')
                AND (:admin OR f.owner_id=:uid OR EXISTS (
                    SELECT 1 FROM t_file_grant g WHERE g.file_id=f.id AND g.user_id=:uid))"""
                    + (" AND f.id=:file_id" if file_id is not None else ""))
            base_query = """
                SELECT v.id, v.content,
                       v.metadata::jsonb || jsonb_strip_nulls(jsonb_build_object(
                           'documentKey', f.document_key,
                           'documentVersion', f.version_number,
                           'sourceUrl', COALESCE(f.source_url, v.metadata->>'sourceUrl')
                       )) AS metadata,
                       v.embedding <=> CAST(:embedding AS vector) AS distance
                FROM t_vector_store AS v
                JOIN t_ai_customer_service_md_storage AS f
                  ON v.metadata ->> 'mdStorageId' = f.id::text AND f.status = 2
                WHERE """ + scope_clause + """
                  AND (f.pdf_qa_revision=0 OR (v.metadata->>'pdfPage') IS NULL OR EXISTS (
                    SELECT 1 FROM t_pdf_page_audit accepted
                    WHERE accepted.file_id=f.id
                      AND accepted.page_number=CASE WHEN (v.metadata->>'pdfPage') ~ '^[0-9]+$'
                                                    THEN (v.metadata->>'pdfPage')::integer ELSE -1 END
                      AND accepted.status IN ('auto_ok','approved')))
                  AND NOT EXISTS (
                    SELECT 1 FROM t_pdf_page_audit qa
                    WHERE qa.file_id=f.id
                      AND qa.page_number=CASE WHEN (v.metadata->>'pdfPage') ~ '^[0-9]+$'
                                              THEN (v.metadata->>'pdfPage')::integer ELSE -1 END
                      AND qa.status IN ('needs_review','excluded'))
            """
            parameters = {"embedding": vector_literal(vector), "admin": role == "admin", "uid": user_id,
                          "evaluation_file_id": evaluation_file_id, "file_id": file_id}
            vector_rows = db.execute(text(base_query + """
                ORDER BY v.embedding <=> CAST(:embedding AS vector) LIMIT :limit
            """), {**parameters, "limit": CONTEXT_CANDIDATES}).all()
            lexical_rows = []
            if phrases:
                # pg_trgm indexes the ILIKE alternatives; score counts distinct
                # matched fragments. Never let lexical search bypass access scope.
                patterns = [f"%{phrase}%" for phrase in phrases]
                lexical_rows = db.execute(text(base_query + """
                    AND v.content ILIKE ANY(CAST(:patterns AS text[]))
                    ORDER BY (SELECT count(*) FROM unnest(CAST(:patterns AS text[])) AS p(pattern)
                              WHERE v.content ILIKE p.pattern) DESC,
                             v.embedding <=> CAST(:embedding AS vector)
                    LIMIT :lexical_limit
                """), {**parameters, "patterns": patterns, "lexical_limit": LEXICAL_CANDIDATES}).all()
            rows = _fuse_candidates(vector_rows, lexical_rows)
            eligible_rows = [row for row in rows if _matches_explicit_product(part, row)]
            part_contexts = select_relevant_context(part, eligible_rows)
            if trace is not None:
                scored_pdf = rank_pdf_passages(part, [row for row in eligible_rows
                    if row.metadata.get("pdfPage") and float(row.distance) <= MAX_CONTEXT_DISTANCE])
                pdf_scores = {item["row"].content: round(item["score"], 6) for item in scored_pdf}
                trace["candidates"].append({"question": part, "rows": [
                    {"rank": rank, "distance": float(row.distance), "content": row.content,
                     "metadata": row.metadata,
                     "selectedForPart": row.content in part_contexts,
                     "productMatched": _matches_explicit_product(part, row),
                     "withinDistanceGate": float(row.distance) <= MAX_CONTEXT_DISTANCE,
                     "pdfRerankScore": pdf_scores.get(row.content),
                     "topicMatched": _supports_question_topic(part, row.content, row.metadata)}
                     | {"channels": row.channels}
                    for rank, row in enumerate(rows, 1)]})
            metadata_by_content = {row.content: row.metadata for row in rows}
            for content in part_contexts[:3 if len(questions) > 1 else CONTEXT_LIMIT]:
                if content not in seen:
                    contexts.append({"content": content, "metadata": metadata_by_content.get(content) or {}})
                    seen.add(content)
                    if trace is not None:
                        trace["selected"] = list(contexts)
                if len(contexts) >= CONTEXT_LIMIT:
                    return contexts
    return contexts
