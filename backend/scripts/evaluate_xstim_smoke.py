"""Read-only, real-SSE regression questions for the published FDA Xstim manual.

The expected facts/pages were checked against the manufacturer PDF. This small
smoke set is not a medical accuracy benchmark or a substitute for blind review.
"""
import argparse
import json
from dataclasses import dataclass
import unicodedata

from fastapi.testclient import TestClient

from app.main import app
from scripts.auth_helpers import login_client


@dataclass(frozen=True)
class Case:
    name: str
    question: str
    page_groups: tuple[tuple[int, ...], ...] = ()
    facts: tuple[str, ...] = ()
    no_sources: bool = False


CASES = (
    Case("storage-and-transport", "Xstim 的存储和运输温度是多少？", ((3,),), ("-29", "60")),
    Case("storage-paraphrase", "Xstim 不使用时，正常储存温度范围是多少？", ((3,),), ("-29", "60")),
    Case("operating-temperature", "Xstim 正常工作温度范围是多少？", ((3, 18),), ("5", "40")),
    Case("english-storage", "What is the normal storage and transport temperature for Xstim?",
         ((3,),), ("-29", "60")),
    Case("daily-treatment", "Xstim 推荐每天治疗多少小时，设备设计支持多少天？",
         ((6,),), ("24", "270")),
    Case("two-page-question", "Xstim 的工作温度是多少，同时每天建议治疗多少小时？",
         ((3, 18), (6,)), ("5", "40", "24")),
    Case("shower-warning", "使用 Xstim 时能洗澡或游泳吗？", ((3, 6),)),
    Case("pacemaker-warning", "装有心脏起搏器的人能直接使用 Xstim 吗？", ((6,),)),
    Case("other-device-isolation", "ActaStim-S 的存储温度是多少？", no_sources=True),
    Case("out-of-domain", "请用 Python 写冒泡排序", no_sources=True),
)


def run_case(client: TestClient, headers: dict, case: Case, file_id: int) -> dict:
    response = client.post("/customer-service/completion", json={"message": case.question, "fileId": file_id}, headers=headers)
    response.raise_for_status()
    if "event: error" in response.text or "event: close" not in response.text:
        raise AssertionError("SSE error or missing close event")
    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    cards = [card for frame in frames for card in frame.get("items", [])]
    answer = "".join(frame.get("v", "") for frame in frames)
    normalized_answer = unicodedata.normalize("NFKC", answer).translate(
        str.maketrans({"−": "-", "–": "-", "—": "-"}))
    pages = [int(card["citation"].split("第 ")[1].split(" 页")[0]) for card in cards
             if "第 " in card.get("citation", "") and " 页" in card["citation"]]
    errors = []
    if case.no_sources:
        if cards or "不足以回答" not in answer:
            errors.append("应拒答且不返回来源")
    else:
        if not cards or any(card.get("fileName") != "Xstim_P230025D.pdf" for card in cards):
            errors.append("来源不是 Xstim 手册")
        for group in case.page_groups:
            if not any(page in pages for page in group):
                errors.append(f"缺少证据页 {group}")
        for fact in case.facts:
            if fact not in normalized_answer:
                errors.append(f"回答缺少 {fact}")
    return {"case": case.name, "question": case.question, "pages": pages,
            "answer": answer, "passed": not errors, "errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", help="Run case names only")
    args = parser.parse_args()
    selected_cases = tuple(case for case in CASES if not args.only or case.name in args.only)
    if not selected_cases:
        parser.error("No matching case names")
    with TestClient(app) as client:
        headers = login_client(client)
        matches = [row for row in client.get("/customer-service/md/available").json()["data"]
                   if row["fileName"] == "Xstim_P230025D.pdf"]
        if len(matches) != 1:
            raise RuntimeError("需要一份已发布且可查询的 Xstim_P230025D.pdf")
        file_id = matches[0]["id"]
        results = []
        for case in selected_cases:
            result = run_case(client, headers, case, file_id)
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    failed = [result["case"] for result in results if not result["passed"]]
    print(json.dumps({"total": len(results), "passed": len(results) - len(failed),
                      "failed": failed}, ensure_ascii=False), flush=True)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
