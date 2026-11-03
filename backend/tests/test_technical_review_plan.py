import copy

import pytest

from scripts.apply_accelstim_technical_review import SOURCE, build_plan


def fixture():
    risk = {1,2,3,5,6,7,10,11,12,13,14,16,17,18,19,21,24,25,26,27,31,32,33,34,
            35,36,37,39,40,41,44,45,46,47,48,50,51,52,53,55,58,59,60,61,65,66,67,68,70,71,72}
    evidence = {"fileId": 7, "sourceSha256": SOURCE, "pageCount": 72, "chunkCount": 144,
        "published": False, "pages": [{"physicalPage": n,
        "auditStatus": "needs_review" if n in risk else "auto_ok",
        "indexedChunks": [{"content": "synthetic test, not real evidence"}]} for n in range(1,73)]}
    ledger = {"sourceSha256": SOURCE, "reviews": [{"page": n, "note": "unresolved test evidence"} for n in risk]}
    return evidence, ledger


def test_bounded_plan_keeps_20_pages_blocked_and_names_ai():
    evidence, ledger = fixture()
    plan = build_plan(evidence, ledger)
    assert len(plan) == 51
    assert sum(p["decision"] == "approved" for p in plan) == 24
    assert sum(p["decision"] == "excluded" for p in plan) == 7
    assert sum(p["decision"] == "needs_review" for p in plan) == 20
    assert all(p["note"].startswith("AI技术验收") for p in plan)
    assert all(p["decision"] == "needs_review" for p in plan if p["page"] in (26,27,60,61))


@pytest.mark.parametrize("change", ["hash", "id", "published", "risk", "index"])
def test_stale_or_wrong_evidence_fails_closed(change):
    evidence, ledger = copy.deepcopy(fixture())
    if change == "hash": evidence["sourceSha256"] = "wrong"
    elif change == "id": evidence["fileId"] = 2
    elif change == "published": evidence["published"] = True
    elif change == "risk": evidence["pages"][0]["auditStatus"] = "auto_ok"
    else: evidence["pages"][15]["indexedChunks"] = []
    with pytest.raises(ValueError):
        build_plan(evidence, ledger)
