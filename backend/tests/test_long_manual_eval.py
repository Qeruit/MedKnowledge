"""Gold-page scoring must not reward a plausible answer from the wrong page."""
from evaluation.run_long_manuals import evidence_match, score


def test_pdf_layout_whitespace_does_not_break_gold_phrase():
    case = {"page": 250, "evidenceTerms": ["12-hour", "grace period"]}
    assert evidence_match({"content": "12-hour grace\n period", "metadata": {"pdfPage": 250}}, case)
    assert not evidence_match({"content": "12-hour grace period", "metadata": {"pdfPage": 251}}, case)


def test_candidate_hit_does_not_hide_final_selection_failure():
    case = {"page": 84, "evidenceTerms": ["0%到200%"]}
    good = {"content": "0%到200%", "metadata": {"pdfPage": 84}}
    wrong = {"content": "0%到200%", "metadata": {"pdfPage": 85}}
    result = score(case, {"selected": [wrong], "candidates": [{"rows": [good, wrong]}]})
    assert result["pageRecallAt"]["3"] == 1
    assert not result["passed"]


def test_unrelated_sources_fail_no_evidence_case():
    assert not score({"page": None}, {"selected": [{"content": "device introduction"}]})["passed"]
    assert score({"page": None}, {"selected": []})["passed"]
