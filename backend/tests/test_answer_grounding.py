from evaluation.answer_grounding import CASES, check_answer
from app.answer_prompt import evidence_reply, INCOMPLETE_PROCEDURE_REPLY, NO_EVIDENCE_REPLY, STUDY_DURATION_REPLY


def test_refusal_does_not_excuse_unsupported_trial_duration_claim():
    case = CASES[0]
    assert not check_answer(case, "The document does not specify device life, but it works up to 140 days.")["passed"]
    assert check_answer(case, "The clinical trial lasted 20 weeks; this does not specify maximum device life.")["passed"]


def test_invented_menu_icon_fails_even_with_incomplete_text_notice():
    case = CASES[2]
    assert not check_answer(case, "文字不完整，但先按 ☰ 菜单键。")["passed"]
    assert check_answer(case, "文字中图标缺失，无法确定具体按键，请核对原页。")["passed"]
    assert not check_answer(case, "资料不完整，但可参考这些步骤：\n1. 选择基础率模式。")["passed"]


def test_missing_pdf_ui_steps_are_blocked_before_generation():
    incomplete = {"content": "在主屏幕中按  ，然后选择  。", "metadata": {"pdfPage": 6}}
    assert evidence_reply("怎么暂停操作？", [incomplete]) == INCOMPLETE_PROCEDURE_REPLY
    assert evidence_reply("临时基础率的范围是多少？", [incomplete]) is None
    complete = {"content": "选择基础率，然后按确认按钮。", "metadata": {"pdfPage": 6}}
    assert evidence_reply("应按哪个按钮？", [complete]) is None
    assert evidence_reply("如何操作？", []) == NO_EVIDENCE_REPLY


def test_trial_observation_period_cannot_establish_device_lifespan():
    assert check_answer(CASES[0], STUDY_DURATION_REPLY)["passed"]
    trial = {"content": "The clinical trial continued for 16 weeks until the investigator judged healing."}
    assert evidence_reply("For up to how many days can the device provide treatments?", [trial]) == STUDY_DURATION_REPLY
    assert evidence_reply("How long was the clinical trial?", [trial]) is None
    instruction = {"content": "The device is capable of providing daily treatments for up to 320 days."}
    assert evidence_reply("For up to how many days can the device provide treatments?", [trial, instruction]) is None
