from evaluation.accelstim_repair_cases import CASES
from evaluation.run_accelstim_repair import answer_check, evidence_check, matches


def case(name):
    return next(c for c in CASES if c["id"]==name)


def test_table_evidence_requires_bound_row_not_incidental_numbers():
    bad=[{"content":"Fémur 28 26 2 93%; unrelated 13 12 1 92%", "metadata":{"pdfPage":66}}]
    good=[{"content":"| Femur | 13 | 12 | 1 | 92% |", "metadata":{"pdfPage":66}}]
    assert not evidence_check(case("femur-es"),bad)["passed"]
    assert evidence_check(case("femur-es"),good)["passed"]
    assert not evidence_check(case("femur-es"),[{**good[0],"metadata":{"pdfPage":21}}])["passed"]


def test_json_numbers_must_belong_to_correct_fields_and_conditions():
    gold=case("femur-en")
    assert answer_check(gold,'{"total":13,"healed":12,"failed":1,"healed_percent":"92%"}',[])["passed"]
    assert not answer_check(gold,'{"total":12,"healed":13,"failed":1,"healed_percent":92}',[])["passed"]
    conditions=case("temperature-conditions")
    assert not answer_check(conditions,'{"operating":[-20,60],"transport":[10,35],"storage":[5,30]}',[])["passed"]
    assert not matches({"total":1},{"total":True})


def test_expired_state_cannot_pass_with_fault_action():
    gold=case("e315-only")
    assert answer_check(gold,"E315表示寿命到期，365天。无需检查凝胶或重启。",[])["passed"]
    assert not answer_check(gold,"E315表示寿命到期，365天。请检查凝胶并重启。",[])["passed"]


def test_conflict_check_rejects_quantitative_placement_advice():
    gold=case("formula-cn")
    assert answer_check(gold,"原文存在差异，不能确定，需要厂家确认。",[])["passed"]
    assert not answer_check(gold,"原文存在差异，但设备应保持3米距离，请确认。",[])["passed"]
