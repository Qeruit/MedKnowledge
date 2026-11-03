"""Structure-first Markdown splitting; token counts use a configurable proxy tokenizer."""

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import re

import tiktoken

from app.config import settings

VERSION = "markdown-structure-v1"
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
INLINE = re.compile(r"!?\[[^\n]*?\]\([^\n]*?\)|`[^`\n]*`")
LIST_ITEM = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")


@dataclass(frozen=True)
class Chunk:
    text: str
    metadata: dict


@lru_cache(maxsize=4)
def encoding(name: str):
    return tiktoken.get_encoding(name)


def token_count(text: str, name: str | None = None) -> int:
    return len(encoding(name or settings.rag_token_encoding).encode(text, disallowed_special=()))


def _sections(source: str):
    """Split headings/rules only outside code fences; retain ancestor headings."""
    lines = source.splitlines()
    document_meta = {}
    original_url = re.search(r"(?:原始文件|原始来源)[^\n]*?(https?://[^\s）)]+)", source)
    if original_url:
        document_meta["source_url"] = original_url[1].rstrip("。；;,.")
    offset = 0
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, min(len(lines), 40)) if lines[i].strip() == "---"), None)
        if end and any(re.match(r"^(version|source_url|title):", line) for line in lines[1:end]):
            for line in lines[1:end]:
                match = re.match(r"^(version|source_url|title):\s*(.*?)\s*$", line)
                if match:
                    document_meta[match[1]] = match[2].strip("\"'")
            offset, lines = end + 1, lines[end + 1:]
    headings = []
    body = []
    start = offset + 1
    fence = None
    sections = []

    def flush(end):
        nonlocal body
        text = "\n".join(body).strip()
        if text:
            sections.append((list(headings), text, start, end, document_meta))
        body = []

    for i, line in enumerate(lines, offset + 1):
        marker = FENCE.match(line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
            body.append(line)
            continue
        match = HEADING.match(line) if fence is None else None
        if match:
            flush(i - 1)
            level = len(match[1])
            headings = [item for item in headings if item[0] < level]
            headings.append((level, match[2]))
            start = i
        elif fence is None and RULE.match(line):
            flush(i - 1)
            start = i + 1
        else:
            body.append(line)
    flush(offset + len(lines))
    return sections


def _blocks(body: str):
    """Keep tables, fenced code and standalone images atomic."""
    lines = body.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        start = i
        fence = FENCE.match(lines[i])
        table = "|" in lines[i] and i + 1 < len(lines) and re.match(r"^[\s|:-]+$", lines[i + 1])
        image = bool(re.match(r"^\s*!\[.*\]\(.*\)\s*$", lines[i]))
        list_item = bool(LIST_ITEM.match(lines[i]))
        if fence:
            i += 1
            while i < len(lines):
                closing = FENCE.match(lines[i])
                i += 1
                if closing and closing[1][0] == fence[1][0] and len(closing[1]) >= len(fence[1]):
                    break
        elif table:
            i += 1
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                i += 1
        elif image:
            i += 1
        elif list_item:
            i += 1
            while i < len(lines) and lines[i].strip() and not LIST_ITEM.match(lines[i]) and not FENCE.match(lines[i]):
                i += 1
        else:
            i += 1
            while i < len(lines) and lines[i].strip() and not FENCE.match(lines[i]) and not LIST_ITEM.match(lines[i]):
                i += 1
        blocks.append(("\n".join(lines[start:i]), bool(fence or table or image or list_item)))
    return blocks


def _split_text(text: str, budget: int, overlap: int, name: str):
    """Cut Unicode characters without breaking inline links or losing any source text."""
    spans = [(match.start(), match.end()) for match in INLINE.finditer(text)]
    start = 0
    while start < len(text):
        if token_count(text[start:], name) <= budget:
            yield text[start:]
            break
        low, high = start + 1, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if token_count(text[start:middle], name) <= budget:
                low = middle
            else:
                high = middle - 1
        end = low
        boundaries = [m.end() + start for m in re.finditer(r"[。！？!?；;\n]\s*", text[start:end])]
        if boundaries and boundaries[-1] > start + (end - start) // 2:
            end = boundaries[-1]
        for left, right in spans:
            if left < end < right:
                end = left if left > start else right
                break
        yield text[start:end]
        next_start = end
        if overlap:
            # Bound overlap in characters first, then measure the complete Unicode substring.
            next_start = max(start + 1, end - overlap * 4)
            while next_start < end and token_count(text[next_start:end], name) > overlap:
                next_start += 1
            for left, right in spans:
                if left < next_start < right:
                    next_start = right
                    break
        start = max(start + 1, next_start)


def split_documents(source: str, *, enabled: bool | None = None, chunk_tokens: int | None = None,
                    overlap_tokens: int | None = None, token_encoding: str | None = None) -> list[Chunk]:
    enabled = settings.rag_splitter_enabled if enabled is None else enabled
    target = settings.rag_chunk_tokens if chunk_tokens is None else chunk_tokens
    overlap = settings.rag_overlap_tokens if overlap_tokens is None else overlap_tokens
    name = token_encoding or settings.rag_token_encoding
    if target < 128 or not 0 <= overlap < target // 2:
        raise ValueError("Invalid splitter token budget/overlap")
    chunks = []
    for headings, body, start, end, document_meta in _sections(source):
        path = [title for _, title in headings]
        prefix = ""
        if headings:
            level, title = headings[-1]
            prefix = "#" * level + " " + title + "\n"
            if len(path) > 1:
                prefix += "上级章节：" + " / ".join(path[:-1]) + "\n"
        # Explicit citation notes belong to all children of this section.
        citations = list(dict.fromkeys(re.findall(r"来源：[^\n]+", body)))
        suffix = "\n" + "\n".join(citations) if citations else ""
        overhead = token_count(prefix + suffix, name) + 8
        budget = max(16, target - overhead)
        if not enabled or token_count(prefix + body, name) <= target:
            parts = [body]
        else:
            parts, current = [], ""
            for block, atomic in _blocks(body):
                pieces = [block] if atomic else list(_split_text(block, budget, min(overlap, budget // 3), name))
                for piece in pieces:
                    candidate = current + ("\n\n" if current else "") + piece
                    if current and token_count(candidate, name) > budget:
                        parts.append(current)
                        current = ""
                    current += ("\n\n" if current else "") + piece
            if current:
                parts.append(current)
        section_id = hashlib.sha256(f"{start}:{path}:{body}".encode()).hexdigest()[:24]
        for piece in parts:
            text = prefix + piece
            if suffix and not all(citation in text for citation in citations):
                text += suffix
            source_url = document_meta.get("source_url")
            if not source_url:
                citation_url = re.search(r"https?://[^\s）)]+", "\n".join(citations))
                source_url = citation_url[0].rstrip("。；;,.") if citation_url else None
            chunks.append(Chunk(text.strip(), {
                "headingPath": path, "sectionTitle": path[-1] if path else document_meta.get("title"),
                "parentSectionId": section_id, "sectionLineStart": start, "sectionLineEnd": end,
                "documentVersion": document_meta.get("version"), "sourceUrl": source_url,
                "citations": citations, "tokenCount": token_count(text.strip(), name),
                "pageReferences": list(dict.fromkeys(re.findall(r"(?:PDF\s*)?第\s*[\d、，,\-–至 ]+\s*页", "\n".join(citations)))),
                "oversized": enabled and token_count(text.strip(), name) > target,
                "splitterVersion": VERSION, "splitterEnabled": enabled,
                "chunkTokens": target, "overlapTokens": overlap, "tokenEncoding": name,
                "chunkIndex": len(chunks),
            }))
    return chunks
