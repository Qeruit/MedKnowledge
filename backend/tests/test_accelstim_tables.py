from pathlib import Path
import re

import pytest

from app.accelstim_tables import SOURCE_SHA256, REPAIRED_PAGES, clinical, repair_page, RF_DISTANCE, span
from app.documents import read_documents


def test_profile_never_changes_another_source_or_unknown_page():
    assert repair_page("different-hash", 65, "raw text") is None
    assert repair_page(SOURCE_SHA256, 7, "raw text") is None
    with pytest.raises(ValueError):
        repair_page(SOURCE_SHA256, 65, "wrong page")


def test_source_anchor_ambiguity_fails_closed():
    assert span("before START \n words END tail", "START words", "END", "repair") == "before repair\n\nEND tail"
    with pytest.raises(ValueError):
        span("START END START END", "START", "END", "repair")
    assert span("before\n         START table END tail", "START", "END", "| column |\n| --- |") == "before\n| column |\n| --- |\n\nEND tail"


def test_clinical_rows_are_bound_to_their_own_categories():
    p65 = clinical(65)
    assert "| <82 days | 9 | 9 | 0 | 100% |" in p65
    assert "| 83-365 days | 39 | 34 | 5 | 87% |" in p65
    p66 = clinical(66)
    assert "| Tibia/Tibia-Fibula/Fibula | 28 | 26 | 2 | 93% |" in p66
    assert "| Femur | 13 | 12 | 1 | 92% |" in p66
    p67 = clinical(67)
    assert "### 12 Fixation: IM rod" in p67
    assert "| No | 43 | 38 | 5 | 88% |" in p67
    assert "Group-level p-value" in p67
    assert "| Missing | (22) | (17) | (5) | not reported |" in p67


def test_math_is_explicit_and_source_difference_is_not_silently_corrected():
    assert "d=1.2*sqrt(P)" in RF_DISTANCE and "d=2.3*sqrt(P)" in RF_DISTANCE
    original = "Rated Maximum table contents For transmitters footnote and CLINICAL STUDIES"
    fixed = repair_page(SOURCE_SHA256, 27, original)
    assert "source discrepancy is preserved" in fixed
    assert "2.7 GHz" in fixed and "2.5 GHz" in fixed
    assert "footnote and CLINICAL STUDIES" in fixed


def test_conflicting_source_formula_does_not_generate_placement_advice():
    from app.answer_prompt import evidence_reply, SOURCE_CONFLICT_REPLY
    documents = [{"content": RF_DISTANCE, "metadata": {"pdfRepairSourceConflict": True}}]
    assert evidence_reply("手机应离设备多少距离？", documents) == SOURCE_CONFLICT_REPLY
    assert evidence_reply("Calculate the separation distance for 2 W", documents) == SOURCE_CONFLICT_REPLY
    assert evidence_reply("说明书提供了哪些临床研究？", documents) is None


def test_real_source_reader_preserves_qa_and_row_integrity():
    path = Path(__file__).resolve().parents[1] / "data/public_manuals/AccelStim_RevAB_2026.pdf"
    if not path.is_file():
        pytest.skip("Public source fixture not installed")
    audits = []
    chunks = read_documents(path, path.name, page_audits=audits)
    patched = {c.metadata["pdfPage"] for c in chunks if c.metadata.get("pdfRepairRevision")}
    assert patched == REPAIRED_PAGES
    for audit in audits:
        if audit["pageNumber"] in patched:
            assert audit["status"] == "needs_review"
            assert "ai_assisted_repair_pending_confirmation" in audit["flags"]
    for chunk in chunks:
        if chunk.metadata.get("pdfRepairRevision"):
            assert chunk.metadata["pdfRepairReviewMode"] == "ai_assisted_pending_confirmation"
            assert chunk.metadata["pdfRepairSourceSha256"] == SOURCE_SHA256
    page66 = "\n".join(c.text for c in chunks if c.metadata["pdfPage"] == 66)
    assert "| Femur | 13 | 12 | 1 | 92% |" in page66
    assert not re.search(r"\| Femur \| 28", page66)
