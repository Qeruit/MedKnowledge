"""Small deterministic retrieval regression set; not a clinical accuracy study."""

import asyncio

from app.knowledge import find_context
from scripts.auth_helpers import evaluation_principal

CASES = [
    {"question": "ActaStim-S 的存储温度是多少？", "must": ["-25", "70"]},
    {"question": "ActaStim-S 每天治疗多久？最长疗程多少天？", "must": ["24", "270"]},
    {"question": "ActaStim-S 存储温度是多少？治疗多久？", "must": ["-25", "70", "24", "270"]},
    {"question": "ActaStim-S 橙色回路未闭合提示怎么办？", "must": ["电极", "导线"],
     "headings_include": ["回路未闭合"], "headings_exclude": ["故障"]},
    {"question": "ActaStim-S 电池充电要多久？", "must": ["3 小时"]},
    {"question": "ActaStim-S 孕妇使用安全吗？", "must": ["孕妇", "未研究"]},
    {"question": "请用 Python 写冒泡排序", "empty": True},
]


async def main():
    principal = evaluation_principal()
    print("仅评测该账号获授权且已发布的资料；请先在管理页发布演示资料")
    passed = 0
    for index, case in enumerate(CASES, 1):
        documents = await find_context(case["question"], **principal)
        context = "\n".join(document["content"] for document in documents)
        headings = "\n".join(document["content"].splitlines()[0] for document in documents)
        result = (
            not documents if case.get("empty") else
            all(term in context for term in case["must"])
            and all(term not in context for term in case.get("must_not", []))
            and all(term in headings for term in case.get("headings_include", []))
            and all(term not in headings for term in case.get("headings_exclude", []))
        )
        passed += result
        print(f"{index}: {'PASS' if result else 'FAIL'} | {case['question']} | evidence={len(documents)}")
        if not result:
            print("  selected:", [document["content"].splitlines()[0] for document in documents])
    print(f"Retrieval regression: {passed}/{len(CASES)} passed")
    if passed != len(CASES):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
