"""Evidence-based retrieval scores and conservative answer smoke checks."""
import math


def matches(content, anchor):
    return anchor in content.splitlines()[0] if content else False


def retrieval_scores(case, trace):
    gold = case["evidence"]
    selected = trace["selected"]
    blocks = trace["candidates"]
    rankings = [block["rows"] for block in blocks]
    if not gold:
        return {"emptyExpected": True, "passed": not selected, "selectedCount": len(selected)}
    # Multi-intent candidate recall: union of per-subquestion top-k lists.
    recalls = {}
    for k in (3, 5, 16):
        recalls[str(k)] = sum(any(matches(row["content"], g) for rows in rankings for row in rows[:k])
                              for g in gold) / len(gold)
    ranks = [min((row["rank"] for rows in rankings for row in rows if matches(row["content"], g)), default=0)
             for g in gold]
    covered = sum(any(matches(doc["content"], g) for doc in selected) for g in gold)
    relevant = [int(any(matches(doc["content"], g) for g in gold)) for doc in selected]
    dcg = sum(v / math.log2(i + 2) for i, v in enumerate(relevant))
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(gold), len(selected))))
    return {"candidateRecall": recalls, "meanEvidenceReciprocalRank": sum(1/r if r else 0 for r in ranks)/len(gold),
            "finalEvidenceRecall": covered / len(gold), "finalPrecision": sum(relevant)/max(len(selected), 1),
            "finalNdcg": dcg / ideal if ideal else 0, "goldBestRanks": ranks,
            "passed": covered == len(gold), "selectedCount": len(selected)}


def parse_sse(value):
    import json
    answer, sources, errors, closed = [], [], [], False
    for block in value.replace("\r\n", "\n").split("\n\n"):
        event, data = "", []
        for line in block.splitlines():
            if line.startswith("event:"): event = line[6:].strip()
            if line.startswith("data:"): data.append(line[5:].strip())
        if event == "close": closed = True
        if not data: continue
        parsed = json.loads("\n".join(data))
        if event == "sources": sources.extend(parsed.get("items", []))
        elif event == "error": errors.append(parsed)
        elif isinstance(parsed, dict) and "v" in parsed: answer.append(parsed["v"])
    return {"answer": "".join(answer), "sources": sources, "errors": errors, "closed": closed}


def answer_check(case, result):
    answer = result["answer"]
    missing = [alts for alts in case["facts"] if not any(s.lower() in answer.lower() for s in alts)]
    forbidden = [phrase for phrase in case.get("forbiddenPhrases", []) if phrase in answer]
    refusal = any(word in answer for word in ("资料不足", "不足以", "未提供", "没有提供", "未包含", "未提及", "无法回答", "没有相关", "不包含"))
    passed = not result["errors"] and result["closed"] and bool(answer)
    if case["evidence"]: passed = passed and not missing and not forbidden and bool(result["sources"])
    else: passed = passed and refusal
    return {"passed": bool(passed), "missingFactGroups": missing, "forbiddenPhrasesPresent": forbidden,
            "refusalDetected": refusal,
            "manualReviewRequired": True}
