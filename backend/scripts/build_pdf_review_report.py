"""Offline assembly of explicit AI review notes; never changes PDF QA approval."""
import argparse
from collections import Counter
import json
from pathlib import Path


def compact(value):
    return "".join(value.split()).casefold()


def build(evidence, ledger):
    if ledger["sourceSha256"] != evidence["sourceSha256"] or ledger["fileId"] != evidence["fileId"]:
        raise ValueError("Review ledger does not match source evidence")
    risk = {p["physicalPage"] for p in evidence["pages"] if p["auditStatus"] == "needs_review"}
    reviews = ledger["reviews"]
    if len({r["page"] for r in reviews}) != len(reviews) or {r["page"] for r in reviews} != risk:
        raise ValueError("Each and only each pending risk page must have one review")
    if any(r["category"] not in ledger["categories"] or not r["note"].strip() for r in reviews):
        raise ValueError("Invalid review decision")
    pages = {p["physicalPage"]: p for p in evidence["pages"]}
    # These anchors were checked against rendered original pages. PASS proves
    # preservation of these anchors only, not retrieval/answers/medical safety.
    anchor_specs = [
        (6, "凝胶厚度", ["1-2mm"]),
        (7, "每日时长及设备能力", ["20 minutes each day", "up to 365 days"]),
        (7, "实际疗程决定者", ["physician will determine the overall length of treatment"]),
        (7, "MR警告", ["MR Unsafe"]),
        (13, "清洁前关机断电", ["switched off and disconnected"]),
        (13, "禁止直接喷洒或倾倒", ["Never use any spray products", "Never pour water or liquids"]),
        (14, "充电时不治疗", ["will not deliver treatment while charging"]),
        (14, "极低电量关机", ["power down after 12 seconds"]),
        (18, "三种环境数值存在（不代表表格已合格）", ["10/35°C", "-20/+60°C", "5/30°C"]),
        (21, "电池标称值", ["1100mAh", "4,07Wh"]),
        (41, "西语每日时长与设备能力", ["20 minutos todos los días", "hasta 365 días"]),
        (48, "西语充电时不治疗", ["no aplicará el tratamiento mientras esté cargándose"]),
    ]
    anchors = []
    for number, label, values in anchor_specs:
        page = pages[number]
        indexed = "\n".join(c["content"] for c in page["indexedChunks"])
        anchors.append({"page": number, "check": label, "anchors": values,
            "sourceTextContains": all(compact(v) in compact(page["sourceText"]) for v in values),
            "indexContains": all(compact(v) in compact(indexed) for v in values)})
    return {"reviewType": ledger["reviewType"], "fileId": evidence["fileId"],
        "sourceSha256": evidence["sourceSha256"], "exportedAt": evidence["exportedAt"],
        "pageCount": evidence["pageCount"], "chunkCount": evidence["chunkCount"],
        "visuallyReviewedRiskPages": len(reviews),
        "otherPages": "21 auto_ok pages text-exported but not individually visually signed off in this review",
        "recommendationCounts": dict(Counter(r["category"] for r in reviews)),
        "categories": ledger["categories"], "reviews": reviews, "anchorChecks": anchors,
        "databaseQaUpdated": False, "published": evidence["published"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--ledger", type=Path, default=Path("evaluation/accelstim_page_review.json"))
    args = parser.parse_args()
    evidence = json.loads((args.folder / "page_evidence.json").read_text(encoding="utf-8"))
    ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
    report = build(evidence, ledger)
    counts = report["recommendationCounts"]
    lines = ["# AccelStim 51 页 AI 辅助技术核对报告", "",
        "结论：逐页核对已完成，但当前原始索引**不宜整份无条件发布**。本报告不等同于临床验证、独立人工审批或修改数据库状态。", "",
        f"文件 ID：{report['fileId']}；源 PDF：72 页；现有向量：{report['chunkCount']}；本次视觉核对：51 个待复核物理页。",
        f"SHA-256：`{report['sourceSha256']}`。数据库证据快照时间（UTC）：{report['exportedAt']}。", "",
        "## 方法与范围", "",
        "使用 PDF 技能：将原 PDF 72 页渲染为100dpi PNG，逐页查看51个待复核页的13张对照图，对关键问题页另看单页；与数据库导出的94个片段及pypdf正文核对。源文件哈希与入库记录一致。",
        "21个auto_ok页仅导出正文作上下文，不声称已逐页视觉审核；没有临床专业人员背书。读取数据库采用强制只读事务，无审批、授权、索引修改。",
        "机械字符串比对只用于发现差异：公式和图片本来就可能不在提取文本内，因此‘未发现缺行’不能证明完整。pypdf还出现缺fontTools字体编码告警；结论以原页视觉和实际索引为准。", "",
        "## 分流建议（不是数据库审批状态）", "",
        f"- 建议不作问答证据：{counts.get('exclude_from_answering', 0)}页，主要为封面、目录、空白和封底；组件清单等有效小段可另建结构化知识卡。",
        f"- 可限于已核对文字使用：{counts.get('limited_text_use', 0)}页；不代表整页图像/操作均已通过。",
        f"- 一般问答前需修复或限定范围：{counts.get('repair_before_general_use', 0)}页，涉及图标、公式、表格、截图示例或双栏文献。", "",
        "## 主要问题", "",
        "1. **公式丢失**：物理26、27、60、61页原图的根号P距离公式未完整进入索引，部分只剩系数。不能仅靠保留的频段与数字让模型推导。",
        "2. **行名/数值错配**：西语65页<82对应9/9/0/100%，索引数值在行名前；66页胫骨行28/26/2/93%被放到Fémur同一文本行。67页No/Sí与数值类似错位。",
        "3. **截图示例污染正文**：12、14、16、46、48、50页反复混入59、64、92.2%、14:20、20:00、EXXXXXXXX，不能当患者实际数据或额外治疗参数。",
        "4. **状态步骤迁移风险**：17/51页到期E315、故障E3XX及不支持充电器必须逐行绑定处理方法；检查凝胶/重启不能移用于到期状态。",
        "5. **双栏与图层污染**：19/53页图徽文字切碎插入正文；34/68页左右参考文献混排。",
        "6. **页码与语言**：物理35起为西语部分；物理41印刷7。引用必须明确物理页，不能把西语7与英文7混同，也不能仅凭固定偏移推断全篇对应。", "",
        "## 数值/限定锚点核对", "",
        "以下只是已核对锚点的保留情况，不是检索命中率、回答正确率或51页通过率。没有调用LLM，也未对未发布资料绕过门禁生成公开回答。", "",
        "| 物理页 | 核对项 | 原文锚点 | 索引锚点 |", "|---|---|---|---|"]
    for item in report["anchorChecks"]:
        lines.append(f"| {item['page']} | {item['check']} | {'存在' if item['sourceTextContains'] else '未匹配'} | {'存在' if item['indexContains'] else '未匹配'} |")
    lines += ["", "## 逐页结论", "", "每条链接指向可复核的原页渲染与提取/索引文本。所有页码均为PDF物理页。", "",
        "| 页 | 建议 | 证据与限制 | 核对材料 |", "|---|---|---|---|"]
    labels = {"exclude_from_answering": "建议排除", "limited_text_use": "限文字使用", "repair_before_general_use": "需修复/限定"}
    for item in report["reviews"]:
        n = item["page"]
        lines.append(f"| {n} | {labels[item['category']]} | {item['note']} | [原页](images/page-{n:02d}.png) · [文本/索引](page-{n:02d}.txt) |")
    lines += ["", "## 建议的后续处理", "",
        "先形成窄范围、带来源页码的经核对文字知识卡用于公开演示；若坚持整份原PDF问答，则需先修复表格/公式与截图图层，再重索引新版本并运行质量评测。不要为清零待审数而批量排除所有复杂页。",
        "本报告保留为AI辅助结论。由资料负责人确认适用范围后，才可记录真实页面结论；最终发布仍由独立管理员进行。当前51页数据库状态维持needs_review，文件维持pending_review、未发布。"]
    target = args.folder / "review_report"
    target.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    target.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"reviewed": len(report["reviews"]), "counts": counts,
                      "anchorsPreserved": sum(c["sourceTextContains"] and c["indexContains"] for c in report["anchorChecks"]),
                      "anchorChecks": len(report["anchorChecks"]), "report": str(target.with_suffix('.md'))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
