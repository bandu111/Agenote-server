"""候选按对象和问题动态聚类，只压缩展示，不修改业务事实与证据。"""
import re
import unicodedata


def canonical(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', value).casefold())


def constraints(value):
    return set(re.findall(r'\d+(?:\.\d+)?\s*(?:小时|分钟|天|万元|元|%)|[零〇一二两三四五六七八九十百千万]+(?:小时|分钟|天|万元|元)|[本下这]?(?:周|星期)[一二三四五六日天]', value))


def similarity(a, b):
    grams = lambda s: {s[i:i+2] for i in range(len(s)-1)}
    x, y = grams(a), grams(b)
    return len(x & y) / len(x | y) if x and y else 0


def label(value):
    return canonical(re.sub('是否|具体|什么|尚未|仍需|建议|确认|待核对|补充|进一步|待补全|待评估|不明|缺失|待完善|仍然|说明|明确', '', value))


def aliases(a, b, sources):
    refs_a, refs_b = set(a['evidence_ids']), set(b['evidence_ids'])
    shared = bool(refs_a & refs_b)
    adjacent = any(
        sources[x].get('speaker_id', sources[x].get('speakerId', sources[x].get('speaker'))) ==
        sources[y].get('speaker_id', sources[y].get('speakerId', sources[y].get('speaker'))) and
        abs(sources[x].get('at', 0) - sources[y].get('at', 0)) <= 30000
        for x in refs_a if x in sources for y in refs_b if y in sources)
    if not (shared or adjacent): return False
    topic_a, topic_b = label(a.get('topic_key', '')), label(b.get('topic_key', ''))
    if topic_a != topic_b and similarity(topic_a, topic_b) < .6: return False
    ta, tb = label(a['title']), label(b['title'])
    if min(len(ta), len(tb)) < 3: return False
    title, body = similarity(ta, tb), similarity(label(a['body']), label(b['body']))
    return (title >= .85 and body >= .10) or (title >= .70 and body >= .25)


def cluster_insights(items, sources=None):
    sources = {u['id']: u for u in (sources or [])}
    clusters = []
    for item in items:
        if item.get('attention') == 'defer':
            continue
        topic, issue = item.get('topic_key', ''), item.get('issue_key', '')
        # 旧版本没有语义键时不猜测归属；客户端兼容层另有保守相似度兜底。
        branch = lambda i: i['kind'] == 'prediction' and bool(re.search('分支|替代方案|否则|如果.+则|方案[一二三AB]', i['body']))
        keyed = bool(topic and issue) and item['kind'] != 'stitch' and not branch(item)
        target = None
        if keyed:
            for old in clusters:
                if old['kind'] == 'stitch' or branch(old):
                    continue
                same_key = (canonical(old.get('topic_key', '')), canonical(old.get('issue_key', ''))) == (canonical(topic), canonical(issue))
                if not same_key and not aliases(old, item, sources):
                    continue
                a, b = constraints(old['body']), constraints(item['body'])
                if a and b and a != b:
                    continue
                target = old
                break
        if target is None:
            clusters.append(dict(item))
            continue
        # 保持首张卡片身份；新增依据不会再生成一张同问题卡片。
        priority = lambda i: (i.get('attention') == 'urgent', bool(i.get('impact')), min(len(i['body']), 80))
        preferred = item if priority(item) >= priority(target) else target
        merged = {**preferred, 'id': target['id'],
            'evidence_ids': list(dict.fromkeys([*target['evidence_ids'], *item['evidence_ids']])),
            'knowledge_node_ids': list(dict.fromkeys([*target.get('knowledge_node_ids', []), *item.get('knowledge_node_ids', [])]))}
        merged['evidence_ids'] = merged['evidence_ids'][:1] + merged['evidence_ids'][1:][-19:]
        merged['knowledge_node_ids'] = merged['knowledge_node_ids'][:6]
        target.clear()
        target.update(merged)
    return clusters
