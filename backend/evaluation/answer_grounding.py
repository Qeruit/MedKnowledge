"""Controlled incomplete-evidence cases, using existing public PDF chunks.

These are generation checks with deliberately restricted context, not retrieval
or clinical accuracy measurements. They never change indexed/published data.
"""
import re
import time

from app.answer_prompt import answer_messages, evidence_reply
from app.config import settings
from app.llm import chat_chunks


REFUSAL = re.compile(r"不足|无法确定|不能.{0,8}确定|未(?:说明|提供|明确|给出)|不完整|缺失|"
    r"insufficient|cannot (?:determine|infer)|not (?:specif|provid|stat|enough|establish)|"
    r"does(?:n't| not) (?:specify|provide|state)|cannot be answered|unknown|unable to", re.I)

CASES = (
    {"id": "trial-is-not-device-life", "pages": [30],
     "question": "For up to how many days can AccelStim provide daily treatments?",
     "refusal": True, "facts": [],
     "forbidden": [r"(?:up to|maximum|at most)\s+140\s+days", r"最长.{0,8}140\s*天"]},
    {"id": "timeout-is-not-daily-treatment", "pages": [12],
     "fragmentContains": "automatically timeout after five minutes",
     "question": "AccelStim 每天应佩戴多少分钟？",
     "refusal": True, "facts": [],
     "forbidden": [r"(?:每天|每日).{0,8}(?:5|五)\s*分钟"]},
    {"id": "missing-ui-icons", "file": "MiniMed_780G_SimpleraSync_zh.pdf", "pages": [82],
     "question": "请明确列出 MiniMed 暂停所有输注操作时应按的具体按键名称和菜单图标；不要省略步骤。",
     "refusal": True, "facts": [], "forbidden": [r"(?:按|点击|选择|press|click)\s*[*]*\s*☰",
         r"\n\s*\d+[.、]\s*(?:选择|按|点击|在主屏幕)"]},
    {"id": "price-not-in-warranty", "pages": [20],
     "question": "AccelStim 一台设备的官方零售价是多少？",
     "refusal": True, "facts": [], "forbidden": []},
    {"id": "direct-evidence-duration", "pages": [7],
     "question": "AccelStim 每天应佩戴多少分钟，设备可提供最多多少天的每日治疗？",
     "refusal": False, "facts": ["20", "365"], "forbidden": []},
)


def check_answer(case, answer):
    forbidden = [pattern for pattern in case["forbidden"] if re.search(pattern, answer, re.I)]
    missing = [term for term in case["facts"] if term.casefold() not in answer.casefold()]
    refusal = bool(REFUSAL.search(answer))
    return {"passed": bool(answer) and not forbidden and not missing and (not case["refusal"] or refusal),
            "refusalDetected": refusal, "forbiddenAssertions": forbidden, "missingTerms": missing}


async def evaluate_grounding(files):
    results = []
    for case in CASES:
        filename = case.get("file", "AccelStim_RevAB_2026.pdf")
        item = next(item for item in files if item["file"]["original_file_name"] == filename)
        documents = [{"content": v["content"], "metadata": v["metadata"]} for v in item["vectors"]
                     if v["metadata"].get("pdfPage") in case["pages"]
                     and (not case.get("fragmentContains") or case["fragmentContains"] in v["content"])]
        if not documents:
            raise RuntimeError(f"Grounding fixture page not indexed: {case['id']}")
        started = time.perf_counter()
        error = None
        fallback = evidence_reply(case["question"], documents)
        try:
            answer = fallback or "".join([c async for c in chat_chunks(answer_messages(case["question"], documents),
                                                                      settings.customer_model, 0)])
        except Exception as exc:
            error, answer = type(exc).__name__, ""
        result = {"id": case["id"], "question": case["question"], "file": filename,
                  "pages": case["pages"], "answer": answer, "error": error,
                  "modelCalled": not bool(fallback),
                  **check_answer(case, answer), "elapsedMs": round((time.perf_counter() - started) * 1000, 2),
                  "manualReviewRequired": True}
        results.append(result)
        print(f"grounding {case['id']} {'PASS' if result['passed'] else 'FAIL'}", flush=True)
    return results
