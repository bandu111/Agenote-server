"""跨模块一致的词面相关度，不表示模型置信度、事实正确率或审批结论。"""
import math
import re

METHOD = 'lexical_relevance_v1'


def terms(text):
    values = set()
    for part in re.findall(r'[a-z0-9_+#]+|[\u4e00-\u9fff]+', text.lower()[:12000]):
        if re.fullmatch(r'[\u4e00-\u9fff]+', part):
            values.update(part[i:i+2] for i in range(len(part)-1))
            if len(part) == 1: values.add(part)
        else:
            values.add(part)
    return values


def relevance(query, target):
    a, b = terms(query), terms(target)
    if not a or not b: return None
    shared = len(a & b)
    if not shared: return 0
    # 查询覆盖率抵消长文档长度影响；Dice 保留对多余词与片段差异的惩罚。
    return min(100, math.floor(100 * (0.7 * shared / len(a) + 0.3 * 2 * shared / (len(a)+len(b))) + 0.5))


def excerpt(text, query, size=240):
    if len(text) <= size: return text
    lower = text.lower()
    positions = [lower.find(t) for t in terms(query) if lower.find(t) >= 0]
    start = max(0, min(positions, default=0)-60)
    return ('…' if start else '') + text[start:start+size] + ('…' if start+size < len(text) else '')
