"""Generate equivalent, non-clinical reader samples for upload testing."""
import argparse
from pathlib import Path

from docx import Document
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

FACTS = ["Sample identifier: TEST-FORMAT-2026. These are software test data only.",
         "Sample marker: FINAL-FORMAT-MARKER. No medical instructions are provided."]


def generate(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    markdown = "# File reader test\n\n" + "\n\n---\n\n".join(FACTS)
    (output / "sample.md").write_text(markdown, encoding="utf-8")
    (output / "sample.txt").write_text("\n\n".join(FACTS), encoding="utf-8")
    doc = Document()
    doc.add_heading("File reader test", level=1)
    for fact in FACTS:
        doc.add_paragraph(fact)
    doc.save(output / "sample.docx")
    writer = PdfWriter()
    for fact in FACTS:
        page = writer.add_blank_page(width=600, height=800)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        text = fact.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 10 Tf 30 700 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(output / "sample.pdf")
    return [output / f"sample.{extension}" for extension in ("md", "pdf", "docx", "txt")]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/format_samples"))
    args = parser.parse_args()
    for path in generate(args.output):
        print(path)
