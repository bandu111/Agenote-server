"""只节流只读提示，绝不节流业务提取、撤回审批或行动核验。"""
import re
import time
from collections import OrderedDict
from app.modules.meetings.services.business_guard import transcript_hashes, fingerprint


def features(text):
    value = re.sub(r'[\W_]+', '', text.lower())
    grams = frozenset(fingerprint(value[i:i+2])[:12] for i in range(len(value)-1))
    facts = tuple(sorted(set(re.findall(
        r'\d+(?:[./:%]\d+)*(?:万元|元|天|日|月|小时|%)?|[零〇一二两三四五六七八九十百千万]+(?:万元|元|天|日|月|小时)|'
        r'[本下这]?(?:周|星期)[一二三四五六日天]|windows|android|ios|web|macos|linux|鸿蒙|小程序|'
        r'尚未批准|未批准|已批准|未确认|已确认|不同意|同意|不纳入|纳入|无需|不需要|考虑|建议|可能|确定|取消|撤回|改为|不再|不要|改期', text.lower()))))
    return grams, facts


def redundant_reasoning(sources, context):
    """仅识别同一声源新增的高度重复话语，业务状态提取仍照常执行。"""
    from app.modules.meetings.services.incremental_analysis import changed_sources
    from app.modules.meetings.services.commitment_agent import source_speaker
    previous = (context or {}).get('previous_analysis', {}).get('source_hashes', {})
    delta = changed_sources(sources, context)
    if not previous or not delta or (context or {}).get('knowledge_changed'):
        return False
    for item in delta:
        if item['id'] in previous:
            return False
        a, facts = features(item.get('correction') or item['raw'])
        matches = [u for u in sources if u['id'] in previous and source_speaker(u) == source_speaker(item)]
        if not any(facts == old_facts and a and b and len(a & b)/len(a | b) >= .8
            for b, old_facts in (features(u.get('correction') or u['raw']) for u in matches[-24:])):
            return False
    return True


class HintGate:
    def __init__(self, clock=time.monotonic):
        self.clock, self.states = clock, OrderedDict()

    def prepare(self, owner, meeting, sources, roles=None):
        now = self.clock()
        self.states = OrderedDict((k, v) for k, v in self.states.items() if now-v['at'] < 600)
        key = (owner, meeting or sources[0]['id'])
        previous = self.states.get(key, {'hashes': {}, 'recent': []})
        hashes = transcript_hashes(sources)
        delta = [u for u in sources if previous['hashes'].get(u['id']) != hashes[u['id']]]
        focus = (delta or sources)[-1]
        from app.modules.meetings.services.meeting_agents import analysis_triggers
        needed = bool(delta) and bool(analysis_triggers(delta) & (
            roles if roles is not None else {'memory', 'forecast', 'risk', 'commitment', 'planner'}))
        # 同一 ID 的纠错或声源修订必须重新检查，不受相似文本节流影响。
        revised = any(u['id'] in previous['hashes'] for u in delta)
        text = '\n'.join((u.get('correction') or u['raw']) for u in delta)
        signature = features(text)
        if needed and not revised:
            for old, at in previous['recent']:
                if now-at > 30 or signature[1] != old[1]:
                    continue
                a, b = signature[0], old[0]
                if a and b and len(a & b)/len(a | b) >= .72:
                    needed = False
                    break
        return {'key': key, 'hashes': hashes, 'recent': previous['recent'],
            'signature': signature, 'focus_id': focus['id'], 'delta_ids': [u['id'] for u in delta],
            'needed': needed, 'at': now}

    def remember(self, decision, generated=False):
        recent = decision['recent']
        if generated:
            recent = [*recent, (decision['signature'], self.clock())][-8:]
        self.states[decision['key']] = {'hashes': decision['hashes'], 'recent': recent, 'at': self.clock()}
        self.states.move_to_end(decision['key'])
        while len(self.states) > 256:
            self.states.popitem(last=False)
