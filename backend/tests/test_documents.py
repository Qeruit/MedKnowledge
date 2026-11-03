from pathlib import Path
from zipfile import ZipFile

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.documents import DocumentReadError, read_documents, validate_document


def make_pdf(path: Path, texts=("FIRST PAGE TEST FACT", "SECOND PAGE TEST FACT")):
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=600, height=800)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): writer._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(path)


def test_pdf_page_evidence_and_no_loss(tmp_path):
    path = tmp_path / "manual.pdf"
    make_pdf(path)
    chunks = read_documents(path, path.name)
    assert len(chunks) == 2
    assert [c.metadata["pdfPage"] for c in chunks] == [1, 2]
    assert all(c.metadata["fileFormat"] == "pdf" for c in chunks)
    assert "FIRST PAGE TEST FACT" in chunks[0].text and "SECOND PAGE TEST FACT" in chunks[1].text
    assert "PDF 第 2 页" in chunks[1].text


def test_pdf_page_audit_tracks_exact_pages_and_short_text(tmp_path):
    path = tmp_path / "short-manual.pdf"
    make_pdf(path, texts=("SHORT FACT", "SECOND PAGE TEST FACT"))
    pages = []
    chunks = read_documents(path, path.name, page_audits=pages)
    assert [item["pageNumber"] for item in pages] == [1, 2]
    assert pages[0]["extractedText"] == "SHORT FACT"
    assert pages[0]["status"] == "needs_review"
    assert "short_extraction" in pages[0]["flags"]
    assert [chunk.metadata["pdfPage"] for chunk in chunks] == [1, 2]


def test_pdf_empty_content_page_is_skipped_without_losing_page_number(tmp_path):
    path = tmp_path / "manual-with-blank.pdf"
    make_pdf(path)
    reader = PdfWriter()
    blank = reader.add_blank_page(width=600, height=800)
    empty_content = DecodedStreamObject()
    empty_content.set_data(b"")
    blank[NameObject("/Contents")] = reader._add_object(empty_content)
    reader.append(path)
    reader.write(path)
    pages = []
    chunks = read_documents(path, path.name, page_audits=pages)
    assert [chunk.metadata["pdfPage"] for chunk in chunks] == [2, 3]
    assert pages[0]["status"] == "excluded" and pages[0]["pageNumber"] == 1


def test_docx_titles_tables_and_hyperlinks(tmp_path):
    path = tmp_path / "manual.docx"
    doc = Document()
    doc.add_heading("测试说明书", level=1)
    doc.add_heading("技术参数", level=2)
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "参数"
    table.cell(0, 1).text = "测试值"
    table.cell(1, 0).text = "TEST-MODEL"
    table.cell(1, 1).text = "MODEL-123"
    paragraph = doc.add_paragraph("原始链接：")
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), paragraph.part.relate_to("https://example.com/manual.pdf", RELATIONSHIP_TYPE.HYPERLINK, is_external=True))
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = "原说明书"
    run.append(text)
    link.append(run)
    paragraph._p.append(link)
    doc.save(path)
    chunks = read_documents(path, path.name)
    assert "| TEST-MODEL | MODEL-123 |" in chunks[0].text
    assert "[原说明书](https://example.com/manual.pdf)" in chunks[0].text
    assert chunks[0].metadata["headingPath"] == ["测试说明书", "技术参数"]


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "gb18030"])
def test_txt_encoding_is_recorded(tmp_path, encoding):
    path = tmp_path / "manual.txt"
    path.write_bytes("测试资料：型号 TEST-123，不用于实际器械操作。".encode(encoding))
    chunks = read_documents(path, path.name)
    assert "测试资料" in chunks[0].text and "TEST-123" in chunks[0].text
    assert chunks[0].metadata["textEncoding"] == encoding


def test_reject_fake_pdf_docx_and_binary_text(tmp_path):
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_text("不是 PDF", encoding="utf-8")
    with pytest.raises(DocumentReadError):
        validate_document(fake_pdf, fake_pdf.name)
    fake_docx = tmp_path / "fake.docx"
    with ZipFile(fake_docx, "w") as archive:
        archive.writestr("anything.txt", "not Word")
    with pytest.raises(DocumentReadError):
        validate_document(fake_docx, fake_docx.name)
    binary = tmp_path / "fake.txt"
    binary.write_bytes(b"hello\x00binary")
    with pytest.raises(DocumentReadError):
        validate_document(binary, binary.name)


def test_encrypted_and_nontext_pdf_are_not_reported_as_complete(tmp_path):
    path = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.encrypt("password")
    writer.write(path)
    with pytest.raises(DocumentReadError, match="加密"):
        read_documents(path, path.name)
    path = tmp_path / "drawing.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    content = DecodedStreamObject()
    content.set_data(b"10 10 50 50 re S")
    page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(path)
    with pytest.raises(DocumentReadError, match="OCR"):
        read_documents(path, path.name)
