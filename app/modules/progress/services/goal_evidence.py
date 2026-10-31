"""交付目标与关联的确定性证据检查；不将自然表达等同于正式批准。"""
import re


def certain_clause(text, quote):
    # 模型逐字引用通常带句号；分句去掉分隔符后仍应能定位同一句证据。
    quote = (quote or '').strip().rstrip('。；;！？?').strip()
    if not quote:
        return False
    clauses = re.split(r"[。；;！？?\n]", text)
    for clause in clauses:
        if quote not in clause:
            continue
        if re.search(r"如果|假如|考虑|建议|也许|可能|争取|暂定|还没确定|尚未确认|尚未批准|未获批准|取消不了", clause):
            continue
        before = clause[:clause.index(quote)]
        if re.search(r"(?:不要|不再|不做|不需要|不能|无法|撤回|取消).{0,6}$", before):
            continue
        # 问号是整个句子的语气，不能因分句时丢掉问号就变成肯定句。
        if re.search(re.escape(clause) + r"[？?]", text):
            continue
        return True
    return False


def supported_goal(goal, sources):
    origin = sources.get(goal.get("origin_id"))
    ids = goal.get("evidence_ids", [])
    if not origin or origin.get("withdrawn") or origin.get('turn_status', 'complete') != 'complete' or not ids or any(i not in sources or sources[i].get("withdrawn") or sources[i].get('turn_status', 'complete') != 'complete' for i in ids):
        return False
    quote = goal.get("condition", "")
    raw = origin.get("correction") or origin["raw"]
    if len(quote.strip()) < 4 or not certain_clause(raw, quote):
        return False
    words = "\n".join(sources[i].get("correction") or sources[i]["raw"] for i in ids)
    # 不再要求字面出现“目标”；自然项目交付、上线与验收表达也可以形成待确认目标。
    durable = bool(re.search(r"目标|本期|这期|一期|二期|本阶段|这个阶段|项目|交付范围|验收|里程碑|上线|投产", words))
    durable |= bool(re.search(r"交付|部署|发布", words) and re.search(r"平台|客户端|系统|应用|Android|iOS|版本|环境", words, re.I))
    return durable


def explicit_milestones(goal, evidence):
    return [m["title"] for m in goal.get("milestones", [])
        if len(m["title"]) >= 4 and any(certain_clause(raw, m["title"]) for raw in evidence)]
