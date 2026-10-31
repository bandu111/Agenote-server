"""首页只展示当前会议关联的有效目标与进度，不把账户最新目标混入另一场会议。"""
from app.modules.progress.services.goal_continuity import ongoing


def situation(progress, meeting=None, now=None):
    goals = [g for g in progress if g.get('kind') == 'goal' and g.get('evidenceCurrent', True)
        and not g.get('reviewPending') and not g.get('reviewRejected')
        and ongoing(g, now, include_completed=True)]
    if meeting:
        sid = meeting['id']
        goals = [g for g in goals if sid in {g.get('sessionId'), *(
            s.get('sessionId') for s in g.get('meetingSources', {}).values())}]
    # 无明确唯一目标时宁可显示会议状态，不任意取账户中的第一个目标。
    goal = goals[0] if len(goals) == 1 else None
    rows = []
    if meeting:
        sources = {u['id']: u for u in meeting['content'].get('utterances', [])}
        facts = []
        for fact in meeting.get('analysis', {}).get('state', []):
            ids = fact.get('evidence_ids', [])
            if not ids or any(i not in sources or sources[i].get('withdrawn') for i in ids):
                continue
            facts.append(fact)
        # 后续同一话题覆盖先前陈述，撤回项不作为当前进度。
        latest = {f.get('topic', f['value']): f for f in facts}
        for fact in list(latest.values())[-4:]:
            if fact['status'] == 'withdrawn':
                continue
            rows.append({'label': {'confirmed': '会议陈述', 'proposed': '待讨论',
                'unresolved': '待解决'}.get(fact['status'], '待核对'),
                'text': fact['value'], 'evidenceIds': fact['evidence_ids']})
    # 工作态势沿用当前会议的讨论/行动状态；里程碑只在目标详情的拓扑展示。
    if goal and not rows:
        rows = [{'label': '持续目标', 'text': goal['condition'], 'evidenceIds': goal.get('evidenceIds', [])}]
    return {
        'title': goal['title'] if goal else (meeting['content'].get('title', '当前会议') if meeting else ''),
        'rows': rows[:4],
        'situationGoal': {k: v for k, v in goal.items() if k in {
            'id', 'title', 'code', 'phase', 'condition', 'acceptance', 'deadline', 'status', 'milestones', 'projectId'}}
            if goal else None,
    }
