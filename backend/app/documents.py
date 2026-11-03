"""Validated file readers feeding the same structure-aware chunk contract."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import subprocess
from zipfile import BadZipFile, ZipFile

import httpx
import olefile
from docx import Document as WordDocument
from docx.table import Table
from pypdf import PdfReader

from app.config import settings
from app.splitter import Chunk, split_documents
from app.accelstim_tables import repair_page, REVISION as PDF_REPAIR_REVISION, PROFILE_SHA256

FORMATS = {"md": "markdown", "markdown": "markdown", "txt": "text", "pdf": "pdf",
           "docx": "docx", "doc": "doc"}
MAX_DOCX_EXPANDED = 100 * 1024 * 1024


class DocumentReadError(ValueError):
    pass


def document_format(filename: str) -> str:
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension not in FORMATS:
        raise DocumentReadError("仅支持 Markdown、PDF、DOCX、DOC 和 TXT 文件")
    return FORMATS[extension]


def require_format_available(filename: str):
    if document_format(filename) == "doc" and not settings.tika_server_url:
        raise DocumentReadError("旧版 DOC 需要配置 TIKA_SERVER_URL；也可另存为 DOCX 后上传")


def _decode_text(path: Path, markdown: bool = False) -> tuple[str, str]:
    raw = path.read_bytes()
    encodings = ["utf-8-sig"] if markdown else (
        ["utf-16"] if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig", "gb18030"])
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
        except UnicodeError:
            continue
        if "\x00" in text or any(ord(c) < 32 and c not in "\n\r\t\f" for c in text):
            raise DocumentReadError("文本含二进制控制字符，请核对文件格式")
        if len(text) > settings.document_max_chars:
            raise DocumentReadError("解析文本超过 DOCUMENT_MAX_CHARS 限制")
        if not text.strip():
            raise DocumentReadError("文件没有可读取的正文")
        return text, encoding
    raise DocumentReadError("Markdown 必须为 UTF-8；TXT 支持 UTF-8、带 BOM 的 UTF-16 或 GB18030")


def validate_document(path: Path, filename: str):
    """Check bytes as well as the extension before registering the completed file."""
    kind = document_format(filename)
    with path.open("rb") as source:
        header = source.read(8)
    if kind in {"markdown", "text"}:
        _decode_text(path, kind == "markdown")
    elif kind == "pdf":
        if not header.startswith(b"%PDF-"):
            raise DocumentReadError("文件内容不是有效的 PDF，不能仅修改扩展名")
    elif kind == "docx":
        try:
            with ZipFile(path) as archive:
                names = set(archive.namelist())
                if not {"[Content_Types].xml", "word/document.xml"}.issubset(names):
                    raise DocumentReadError("文件不是 DOCX Word 文档")
                if len(names) > 10000 or sum(info.file_size for info in archive.infolist()) > MAX_DOCX_EXPANDED:
                    raise DocumentReadError("DOCX 解压内容超过处理限制")
        except BadZipFile as exc:
            raise DocumentReadError("DOCX 文件损坏或格式错误") from exc
    else:
        require_format_available(filename)
        if header != bytes.fromhex("d0cf11e0a1b11ae1"):
            raise DocumentReadError("文件内容不是旧版 DOC")
        try:
            with olefile.OleFileIO(path) as archive:
                if not archive.exists("WordDocument"):
                    raise DocumentReadError("OLE 文件中没有 WordDocument 正文")
        except (OSError, ValueError) as exc:
            raise DocumentReadError("DOC 文件损坏") from exc


@dataclass
class Unit:
    text: str
    metadata: dict


def _is_blank_pdf_content(page, contents) -> bool:
    """Recognize empty/layout filler pages without hiding graphical-only pages."""
    if page.images:
        return False
    painting = {b"Do", b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"sh"}
    for operands, operator in contents.operations:
        if operator in painting:
            return False
        if operator in {b"TJ", b"'", b'"'}:
            return False
        if operator == b"Tj" and (len(operands) != 1 or str(operands[0]).strip("\x00\x03 \t\r\n")):
            return False
    return True


def _read_pdf(path: Path, filename: str, page_audits: list[dict] | None = None) -> list[Unit]:
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise DocumentReadError("暂不支持加密 PDF，请先提供未加密副本")
    if len(reader.pages) > settings.document_max_pages:
        raise DocumentReadError("PDF 页数超过 DOCUMENT_MAX_PAGES 限制")
    units = []
    total = 0
    labels = reader.page_labels
    with path.open("rb") as source:
        source_sha = hashlib.file_digest(source, "sha256").hexdigest()
    skipped_image_cover = False
    for index, page in enumerate(reader.pages, 1):
        page_label = str(labels[index - 1]) if index - 1 < len(labels) else str(index)
        contents = page.get_contents()
        content_data = contents.get_data() if contents is not None else b""
        if len(content_data) > 20 * 1024 * 1024:
            raise DocumentReadError(f"PDF 第 {index} 页内容流过大，需先拆分文件")
        text = (page.extract_text(extraction_mode="layout") or "").strip()
        used_fallback = False
        if not text:
            if content_data:
                if index == 1 and page.images:
                    # A decorative image-only cover is not evidence. Keep PDF page
                    # numbering intact and disclose that it was not OCR'd.
                    skipped_image_cover = True
                    if page_audits is not None:
                        page_audits.append({"pageNumber": index, "pageLabel": page_label,
                                            "extractedText": "", "flags": ["image_only_cover"], "status": "excluded"})
                    continue
                if _is_blank_pdf_content(page, contents):
                    if page_audits is not None:
                        page_audits.append({"pageNumber": index, "pageLabel": page_label,
                                            "extractedText": "", "flags": ["blank_page"], "status": "excluded"})
                    continue
                # Poppler can decode some rotated fonts that pypdf's layout
                # mode omits. It does not OCR scanned/image-only pages.
                try:
                    fallback = subprocess.run(
                        ["pdftotext", "-f", str(index), "-l", str(index), "-layout", str(path), "-"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        check=True, timeout=30,
                    )
                    text = fallback.stdout.strip()
                    used_fallback = bool(text)
                except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    text = ""
                if not text or "\ufffd" in text:
                    raise DocumentReadError(f"PDF 第 {index} 页无可提取文本，可能是扫描或图像页面；请先 OCR 后上传")
            if page_audits is not None:
                page_audits.append({"pageNumber": index, "pageLabel": page_label,
                                    "extractedText": "", "flags": ["blank_page"], "status": "excluded"})
            continue
        total += len(text)
        if total > settings.document_max_chars:
            raise DocumentReadError("PDF 解析正文超过 DOCUMENT_MAX_CHARS 限制")
        urls = []
        for annotation in page.get("/Annots", []):
            action = annotation.get_object().get("/A")
            if action and str(action.get("/URI", "")).startswith(("https://", "http://")):
                urls.append(str(action["/URI"]))
        if urls:
            text += "\n\n页面链接：\n" + "\n".join(dict.fromkeys(urls))
        repaired = repair_page(source_sha, index, text)
        if repaired is not None:
            text = repaired
        if page_audits is not None:
            # Heuristic triage, not OCR or proof of medical correctness. Preserve
            # the extracted text for a side-by-side comparison with the PDF page.
            flags = []
            if page.images:
                flags.append("embedded_image_unread")
            if len(text.strip()) < 180:
                flags.append("short_extraction")
            if used_fallback:
                flags.append("fallback_extraction")
            if repaired is not None:
                # Text repair is not a human sign-off. Never silently clear QA.
                flags.append("ai_assisted_repair_pending_confirmation")
            if sum(bool(re.search(r"\S\s{4,}\S", line)) for line in text.splitlines()) >= 3:
                flags.append("tabular_or_multicolumn_layout")
            page_audits.append({"pageNumber": index, "pageLabel": page_label,
                                "extractedText": text, "flags": flags,
                                "status": "needs_review" if flags else "auto_ok"})
        citation = f"来源：原文件 PDF 第 {index} 页。"
        units.append(Unit(f"# {filename} — PDF 第 {index} 页\n\n{text}\n\n{citation}", {
            "pdfPage": index, "pdfPageLabel": labels[index - 1], "pdfPageCount": len(reader.pages),
            "pageReferences": [f"PDF 第 {index} 页"], "citations": [citation],
            "pageLinkUrls": list(dict.fromkeys(urls)),
            "reader": "pypdf-layout", "readerWarnings": [
                "PDF 按页提取文字，图像内容未 OCR，复杂表格需人工核对",
                *(["PDF 第 1 页为图像封面，已跳过且未 OCR"] if skipped_image_cover else []),
                *(["表格/公式采用绑定源文件哈希的 AI 辅助转写；仍需独立确认，不代表医疗或人工审批"] if repaired is not None else []),
            ],
            **({"pdfRepairRevision": PDF_REPAIR_REVISION, "pdfRepairSourceSha256": source_sha,
                "pdfRepairProfileSha256": PROFILE_SHA256,
                "pdfRepairReviewMode": "ai_assisted_pending_confirmation",
                "pdfRepairSourceConflict": index in (26,27,60,61)} if repaired is not None else {}),
        }))
    return units


def _paragraph_text(paragraph) -> str:
    parts = []
    for item in paragraph.iter_inner_content():
        address = getattr(item, "address", None)
        if address and address.startswith(("https://", "http://")):
            parts.append(f"[{item.text}]({address})")
        else:
            parts.append(item.text)
    return "".join(parts)


def _read_docx(path: Path) -> list[Unit]:
    document = WordDocument(path)
    blocks = []
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            rows = [[cell.text.replace("|", "\\|").replace("\n", "<br>") for cell in row.cells] for row in block.rows]
            if rows:
                blocks.append("\n".join(["| " + " | ".join(rows[0]) + " |",
                                          "| " + " | ".join("---" for _ in rows[0]) + " |"] +
                                         ["| " + " | ".join(row) + " |" for row in rows[1:]]))
        else:
            text = _paragraph_text(block).strip()
            if not text:
                continue
            style = block.style.name if block.style else ""
            heading = re.search(r"(?:Heading|标题)\s*(\d)", style, re.IGNORECASE)
            if heading:
                text = "#" * min(int(heading[1]), 6) + " " + text
            elif style.lower().startswith("list"):
                text = "- " + text
            blocks.append(text)
    text = "\n\n".join(blocks)
    warnings = ["DOCX 页码取决于排版，当前保留章节而不推断页码"]
    if len(document.inline_shapes):
        warnings.append(f"文档含 {len(document.inline_shapes)} 个嵌入图形，未做图像理解或创建公开图片地址")
    return [Unit(text, {"reader": "python-docx", "readerWarnings": warnings,
                        "documentVersion": document.core_properties.version or None})]


def _read_doc(path: Path) -> list[Unit]:
    require_format_available(path.name)
    try:
        with httpx.Client(timeout=60) as client, path.open("rb") as source:
            response = client.put(settings.tika_server_url + settings.tika_text_path, content=source,
                                  headers={"Content-Type": "application/msword", "Accept": "text/plain"})
            response.raise_for_status()
    except httpx.RequestError as exc:
        raise DocumentReadError("DOC 解析服务未响应，请检查 Tika 后重试") from exc
    except httpx.HTTPStatusError as exc:
        raise DocumentReadError(f"DOC 解析服务返回 HTTP {exc.response.status_code}，请核对文件或 Tika 配置") from exc
    return [Unit(response.text, {"reader": "apache-tika", "readerWarnings": [
        "旧版 DOC 以文本方式提取，不保留排版页码和嵌入图像"]})]


def read_documents(path: Path, filename: str, *, split_options: dict | None = None,
                   page_audits: list[dict] | None = None) -> list[Chunk]:
    validate_document(path, filename)
    kind = document_format(filename)
    if kind in {"markdown", "text"}:
        text, encoding = _decode_text(path, kind == "markdown")
        units = [Unit(text, {"reader": "raw-markdown" if kind == "markdown" else "plain-text",
                             "textEncoding": encoding, "readerWarnings": []})]
    elif kind in {"pdf", "docx"}:
        try:
            units = _read_pdf(path, filename, page_audits) if kind == "pdf" else _read_docx(path)
        except DocumentReadError:
            raise
        except Exception as exc:
            raise DocumentReadError(f"{kind.upper()} 读取失败，请检查文件是否损坏或包含不支持的内容") from exc
    else:
        units = _read_doc(path)
    if sum(len(unit.text) for unit in units) > settings.document_max_chars:
        raise DocumentReadError("解析正文超过 DOCUMENT_MAX_CHARS 限制")
    chunks = []
    for unit in units:
        for chunk in split_documents(unit.text, **(split_options or {})):
            metadata = {**chunk.metadata, **unit.metadata, "fileFormat": kind, "chunkIndex": len(chunks)}
            metadata["lineNumberBasis"] = "originalMarkdown" if kind == "markdown" else "extractedText"
            if metadata.get("documentVersion") is None:
                metadata["documentVersion"] = chunk.metadata.get("documentVersion")
            if "pdfPage" in metadata:
                metadata["parentSectionId"] = f"page-{metadata['pdfPage']}:{chunk.metadata['parentSectionId']}"
            chunks.append(Chunk(chunk.text, metadata))
    if not chunks:
        raise DocumentReadError("文档没有可解析的正文，请检查文件或先完成 OCR")
    return chunks
