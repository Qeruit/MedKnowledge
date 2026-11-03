"""Keep the scorer and fixed evidence labels honest across retrieval changes."""
from types import SimpleNamespace

from evaluation.cases import FACTS, cases
from evaluation.quality import answer_check, parse_sse, retrieval_scores
from app.documents import read_documents
from app.config import PROJECT_ROOT


def test_gold_headings_exist_in_checked_in_corpus():
    source = PROJECT_ROOT / "data" / "actastim_s_verified_demo.md"
    documents = read_documents(source, source.name)
    headings = [document.text.splitlines()[0] for document in documents]
    assert len(FACTS) == 15
    assert len(cases()) == 43
    for anchor, _, _ in FACTS:
        assert any(anchor in heading for heading in headings), anchor
    for case in cases():
        for anchor in case["evidence"]:
            assert any(anchor in heading for heading in headings), anchor
    for case in cases():
        if case["id"] in {"multi-1", "multi-5"}:
            assert ["270"] not in case["facts"], "问题只问每日时长，未问最长疗程"


def test_scoring_distinguishes_candidate_recall_from_final_and_refusal():
    trace = {"selected": [{"content": "## 问：治疗多久\n24 小时"}],
             "candidates": [{"rows": [{"rank": 1, "content": "## 问：治疗多久\n24 小时"},
                                      {"rank": 2, "content": "## 问：存储温度\n-25 到 70"}]}]}
    case = {"evidence": ["治疗多久", "存储温度"]}
    result = retrieval_scores(case, trace)
    assert result["candidateRecall"]["3"] == 1
    assert result["finalEvidenceRecall"] == .5
    assert not result["passed"]
    assert retrieval_scores({"evidence": []}, trace)["passed"] is False
    assert retrieval_scores({"evidence": []}, {"selected": [], "candidates": []})["passed"] is True


def test_sse_answer_check_distinguishes_close_error_and_fact_gap():
    streamed = parse_sse('event: sources\ndata: {"items":[{"heading":"证据"}]}\n\n'
                         'data: {"v":"24小时"}\n\nevent: close\ndata: {}\n\n')
    case = {"facts": [["24"], ["270"]], "evidence": ["疗程"]}
    assert streamed["answer"] == "24小时" and streamed["closed"]
    assert answer_check(case, streamed)["missingFactGroups"] == [["270"]]
    assert not answer_check(case, streamed)["passed"]
    assert answer_check({"facts": [], "evidence": []}, parse_sse(
        'data: {"v":"当前检索到的资料不足以回答"}\n\nevent: close\ndata: {}\n\n'))["passed"]
    assert answer_check({"facts": [], "evidence": []}, parse_sse(
        'data: {"v":"当前资料未提及售价"}\n\nevent: close\ndata: {}\n\n'))["passed"]
    pregnancy = {"facts": [["未研究"]], "evidence": ["怀孕"], "forbiddenPhrases": ["不安全。"]}
    unsafe = {**streamed, "answer": "不安全。孕妇使用的安全性未研究。"}
    assert answer_check(pregnancy, unsafe)["forbiddenPhrasesPresent"] == ["不安全。"]
    assert not answer_check(pregnancy, unsafe)["passed"]
