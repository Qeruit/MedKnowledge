import copy
import json
from pathlib import Path

import pytest

from scripts.build_pdf_review_report import build


def fixture():
    ledger = json.loads((Path(__file__).resolve().parents[1] /
                        "evaluation/accelstim_page_review.json").read_text(encoding="utf-8"))
    # Minimal fixture deliberately lacks the anchors, to avoid confusing an
    # offline self-test with a real PDF content check.
    evidence = {"fileId": ledger["fileId"], "sourceSha256": ledger["sourceSha256"],
        "exportedAt": "test", "pageCount": 72, "chunkCount": 94, "published": False,
        "pages": [{"physicalPage": n, "auditStatus": "needs_review" if n in
                   {r["page"] for r in ledger["reviews"]} else "auto_ok",
                   "sourceText": "", "indexedChunks": []} for n in range(1, 73)]}
    return evidence, ledger


def test_all_51_risk_pages_have_explicit_nonapproval_recommendation():
    evidence, ledger = fixture()
    result = build(evidence, ledger)
    assert result["visuallyReviewedRiskPages"] == 51
    assert result["recommendationCounts"] == {
        "exclude_from_answering": 7, "limited_text_use": 8, "repair_before_general_use": 36}
    assert result["databaseQaUpdated"] is False and result["published"] is False
    assert not any(c["indexContains"] for c in result["anchorChecks"])


@pytest.mark.parametrize("mutation", ["hash", "missing", "duplicate"])
def test_mismatched_or_incomplete_ledger_is_rejected(mutation):
    evidence, ledger = fixture()
    if mutation == "hash":
        ledger["sourceSha256"] = "different"
    elif mutation == "missing":
        ledger["reviews"].pop()
    else:
        ledger["reviews"].append(copy.deepcopy(ledger["reviews"][0]))
    with pytest.raises(ValueError):
        build(evidence, ledger)
