"""Bounded local PDF reranking, without an extra model call or device catalog.

Generated file/page headers are excluded. Score a small body window so a long
page cannot win merely by mentioning many unrelated query words. Attribute
aliases describe common question types, never device names or expected values.
"""
import math
import re
from collections import Counter


STOP_WORDS = set("a an the is are was were be been to of for from in on at by with and or "
                 "how what which when where should can does do it its this that according "
                 "many much up per each during please device guide manual".split())
ALIASES = (
    (("佩戴", "wear", "worn"), r"\b(?:wear|worn|wearing)\b|佩戴"),
    (("每天", "每日", "per day", "each day", "daily"), r"\b(?:daily|day)\b|每天|每日"),
    (("分钟", "minute"), r"\bminutes?\b|分钟"),
    (("小时", "hour"), r"\bhours?\b|小时"),
    (("百分比", "percent", "percentage"), r"%|\bpercent(?:age)?\b|百分比"),
    (("温度", "temperature"), r"\btemperature\b|温度|°[CF]|℃"),
    (("存储", "储存", "storage"), r"\bstorage\b|存储|储存"),
    (("运输", "transport"), r"\btransport(?:ation)?\b|运输"),
    (("充电", "charging", "charge"), r"\bcharg(?:e|ing)\b|充电"),
    (("保修", "质保", "warranty"), r"\bwarrant(?:y|ies|s)\b|保修|质保"),
    (("范围", "上限", "limit", "maximum", "range"),
     r"\b(?:limits?|maximum|range)\b|范围|上限|\d\s*(?:%|°[CF]|℃)?\s*(?:到|至|to|–)\s*\d"),
)
VALUE = re.compile(r"(?:\d+(?:\.\d+)?|\bone|\btwo|\bthree|\bfour|\bfive|\bsix|\bseven|\beight|\bnine|\bten)"
                   r"\s*(?:%|°[CF]|℃|minutes?|hours?|days?|weeks?|months?|years?|"
                   r"units?\s*/\s*hour|cm|mm|分钟|小时|天|个月|年|单位)", re.I)
QUANTITY = re.compile(r"多少|多久|范围|上限|百分比|温度|how (?:long|many|far)|\blimit\b|\brange\b", re.I)


def _triggered(question, trigger):
    if re.search(r"[\u4e00-\u9fff]", trigger):
        return trigger in question
    return bool(re.search(r"\b" + re.escape(trigger) + r"s?\b", question))


def _tokens(value):
    value = value.replace("全部", "所有")
    result = []
    for token in re.findall(r"[a-z]+|\d+|[\u4e00-\u9fff]+", value.casefold()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            result.extend(token[i:i + 2] for i in range(len(token) - 1))
        elif token not in STOP_WORDS:
            result.append(token[:-1] if len(token) > 4 and token.endswith("s") else token)
    return result


def _identity(row):
    name = (row.metadata or {}).get("originalFileName")
    if not name:
        match = re.match(r"#\s*(.*?)\s*—\s*PDF", row.content.splitlines()[0])
        name = match.group(1) if match else ""
    return set(_tokens(name))


def rank_pdf_passages(question, rows):
    """Return scored rows; caller still owns distance/access/page-QA gates."""
    if not rows:
        return []
    bodies = [re.sub(r"\s+", " ", row.content.partition("\n")[2]).strip() for row in rows]
    tokens = [set(_tokens(body)) for body in bodies]
    # Repeated model/file tokens provide scope, not evidence for a parameter.
    file_terms = set().union(*[_identity(row) for row in rows])
    core_question = re.sub(r"如何|怎么|多少|是否|请问|可以|哪些|什么|的|是|吗|呢", " ", question)
    query = set(_tokens(core_question)) - file_terms
    counts = Counter(term for terms in tokens for term in terms)
    weights = {term: 1 + math.log((len(rows) + 1) / (counts[term] + 1)) for term in query}
    denominator = sum(weights.values()) or 1
    aliases = [re.compile(pattern, re.I) for triggers, pattern in ALIASES
               if any(_triggered(question.casefold(), trigger) for trigger in triggers)]
    quantitative = bool(QUANTITY.search(question))
    ranked = []
    for rank, (row, body) in enumerate(zip(rows, bodies, strict=True), 1):
        # Sliding windows tolerate multi-column line breaks while limiting
        # incidental matches across separate topics on the same page.
        windows = [body[i:i + 640] for i in range(0, max(len(body), 1), 320)]
        best = max((
            .55 * sum(weights[t] for t in query & set(_tokens(window))) / denominator
            + .45 * sum(bool(alias.search(window)) for alias in aliases) / max(len(aliases), 1)
            + (.12 if quantitative and VALUE.search(window) else 0)
            for window in windows), default=0)
        score = best + .40 * (1 - float(row.distance)) + .20 / (1 + rank / 4)
        ranked.append({"row": row, "score": score,
                       "queryCoverage": sum(weights[t] for t in query & tokens[rank - 1]) / denominator,
                       "aliasCoverage": sum(bool(alias.search(body)) for alias in aliases) / max(len(aliases), 1),
                       "hasAliases": bool(aliases)})
    return sorted(ranked, key=lambda item: (-item["score"], float(item["row"].distance)))


def select_pdf_passages(question, rows, limit=5):
    ranked = rank_pdf_passages(question, rows)
    if not ranked:
        return []
    # Keep complementary evidence even if its words overlap the first page.
    # Scope and weak-distance filtering have already happened before reranking.
    top = ranked[0]["score"]
    best = ranked[0]
    if not best["hasAliases"] and best["queryCoverage"] >= .90:
        return [best["row"].content]
    eligible = [item for item in ranked if item["score"] >= top - .24
                and not (best["hasAliases"] and best["aliasCoverage"] == 1 and item["aliasCoverage"] < 1)]
    return [item["row"].content for item in eligible[:min(limit, 3)]]
