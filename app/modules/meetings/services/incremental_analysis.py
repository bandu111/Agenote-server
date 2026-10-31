"""按证据版本选择增量输入；缓存不跨账号、授权、声源更正或资料版本复用。"""

import re
from app.modules.meetings.services.business_guard import fingerprint, transcript_hashes


def changed_sources(sources, context=None):
    previous = (context or {}).get('previous_analysis', {})
    hashes = previous.get('source_hashes', {})
    current = transcript_hashes(sources)
    return [u for u in sources if hashes.get(u['id']) != current[u['id']]]


def evidence_ids(item):
    ids = set(item.get('evidence_ids', []))
    if item.get('origin_id'):
        ids.add(item['origin_id'])
    for event in [*item.get('events', []), *item.get('pending_changes', [])]:
        ids.update(event.get('evidence_ids', []))
    return ids


def retained(items, sources, previous):
    """旧来源被删、纠错、撤回或重分声源时，旧候选先失效，不沿用旧审查。"""
    current = transcript_hashes(sources)
    old = previous.get('source_hashes', {})
    withdrawn = {u['id'] for u in sources if u.get('withdrawn')}
    return [item for item in items if evidence_ids(item) and not evidence_ids(item) & withdrawn and all(
        key in current and old.get(key) == current[key] for key in evidence_ids(item))]


def review_binding(candidate, sources, context):
    hashes = transcript_hashes(sources)
    ids = evidence_ids(candidate)
    # 新的明确变更/否定使旧审查失效；关键词只控制缓存，不决定业务撤回。
    controls = {u['id']: hashes[u['id']] for u in sources if re.search(
        r'取消|撤回|改为|改成|改期|延后|不再|不要|不批准|未批准|未同意', u.get('correction') or u['raw'])}
    return fingerprint({'version': 2, 'evidence': {i: hashes.get(i) for i in sorted(ids)},
        'controls': controls, 'knowledge': context.get('knowledge', []),
        'knowledge_revision': context.get('knowledge_revision', 0),
        'progress': context.get('progress', []), 'execution_catalog': context.get('execution_catalog', {}),
        'client_id': context.get('client_id')})


def compact_context(context, sources=None):
    """不把上一轮完整分析、问卷、审查理由重复发送给每个角色。"""
    previous = context.get('previous_analysis', {})
    # 仅携带仍有有效证据的简短问题键，供各角色复用；不重复发送长正文。
    active_issues = previous.get('insights', [])
    if sources is not None:
        active_issues = retained(active_issues, sources, previous)
    if sources is not None:
        previous = {key: retained(previous.get(key, []), sources, previous)
            for key in ['state', 'goals', 'commitments']}
    return {**{k: v for k, v in context.items() if k not in {
        'previous_analysis', 'execution_catalog', 'tool_plans_enabled', 'knowledge_changed'}},
        'current_effective_state': previous.get('state', []),
        'active_issues': [{k: i.get(k, '') for k in ['topic_key', 'issue_key', 'attention', 'impact']}
                         for i in active_issues[-12:] if i.get('topic_key') and i.get('issue_key')],
        'current_goals': previous.get('goals', []),
        'current_commitments': [{k: c.get(k) for k in ['id', 'origin_id', 'action', 'status',
            'side', 'deadline_text', 'missing']} for c in previous.get('commitments', [])]}


def role_sources(sources, context):
    """初次扫描保留全部；后续仅带增量、邻接发言和仍有效事实的原始依据。"""
    previous = context.get('previous_analysis', {})
    if not previous.get('source_hashes'):
        return sources
    delta = changed_sources(sources, context)
    selected = {u['id'] for u in [*delta, *sources[-6:]]}
    for key in ['state', 'goals', 'commitments']:
        for item in previous.get(key, []):
            selected.update(evidence_ids(item))
    # 跨时间的话题拼接依据由 select_context 找回；此处额外保留中断来源。
    for u in sources:
        if u.get('turn_status') == 'interrupted' or any(t in u['raw'] for t in ['…', '...', '[中断]']):
            selected.add(u['id'])
    return [u for u in sources if u['id'] in selected]
