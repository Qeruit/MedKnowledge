"""Offline checks keep multi-turn gold labels and context scoring honest."""

from app.chat_history import contextualize_question
from app.config import PROJECT_ROOT
from app.documents import read_documents
from evaluation.multiturn_cases import SCENARIOS
from evaluation.quality import answer_check, retrieval_scores


def test_multiturn_labels_exist_in_public_corpus():
    source = PROJECT_ROOT / "data" / "actastim_s_verified_demo.md"
    headings = [doc.text.splitlines()[0] for doc in read_documents(source, source.name)]
    assert {scenario["id"] for scenario in SCENARIOS} == {"referent", "topic-switch", "no-evidence"}
    for scenario in SCENARIOS:
        assert len(scenario["turns"]) >= 2
        for turn in scenario["turns"]:
            for anchor in turn["evidence"]:
                assert any(anchor in heading for heading in headings), (scenario["id"], anchor)


def test_followup_uses_prior_question_not_assistant_answer():
    question, previous = contextualize_question(
        "那它最长疗程是多少天？", ["ActaStim-S 电池充满需要多久？", "ActaStim-S 的存储温度是多少？"])
    assert question.startswith("ActaStim-S ")
    assert previous == "ActaStim-S 的存储温度是多少？"
    assert "3 小时" not in question


def test_no_evidence_followup_cannot_pass_with_incidental_source():
    case = SCENARIOS[-1]["turns"][-1]
    irrelevant = {"selected": [{"content": "## 问：ActaStim-S 的存储温度是多少？\n-25 到 70"}],
                  "candidates": []}
    assert not retrieval_scores(case, irrelevant)["passed"]
    streamed = {"answer": "售价为 100 元", "sources": [], "errors": [], "closed": True}
    assert not answer_check(case, streamed)["passed"]
