"""当前会议进展、缺交期展示、跨会议明确续接及到期边界。"""
from datetime import datetime, timezone
from sqlalchemy import select
from app.modules.home.services.home_service import build_home
from app.modules.progress.services.goal_continuity import reported_milestones, mentions, explicit_steps
from app.modules.progress.services.progress_service import ProgressService
from app.modules.progress.models.progress_item import ProgressItem
from app.modules.meetings.models.meeting import Meeting
from test_meetings import account


TITLE = '完成工单系统上线验收'
STEPS = ['需求范围确认', '核心模块开发', '双端联调', '上线验收']


def test_reported_progress_is_not_verified_and_negation_is_retained():
    nodes = [{'title': t, 'status': 'pending', 'weight': 1} for t in STEPS]
    sources = [{'id': 'g', 'raw': '需求范围确认已经完成，核心模块开发正在进行，双端联调还没完成，上线验收如果通过再发布。'}]
    result = reported_milestones(nodes, sources, 'one')
    assert [m['status'] for m in result] == ['completed', 'active', 'pending', 'pending']
    assert result[0]['statusBasis'] == 'meeting_reported'
    assert nodes[0]['status'] == 'pending'
    assert not mentions({'condition': TITLE}, ['不要继续' + TITLE])
    assert explicit_steps(['里程碑包括需求范围确认、核心模块开发。']) == STEPS[:2]
    assert reported_milestones(nodes, [{'id': 'n', 'raw': '需求范围确认不代表已完成。核心模块开发不能确认已完成。'}])[0]['status'] == 'pending'
    assert reported_milestones(nodes, [{'id': 'n', 'raw': '需求范围已经确认。'}])[0]['status'] == 'completed'


async def sync_and_project(client, app, headers, sid, raw, *, with_goal=True, deadline=None, phase=None, steps=None):
    payload = {'id': sid, 'title': '工单推进会', 'revision': 1,
        'startedAt': '2026-10-25T10:00:00+08:00', 'elapsed': 60000, 'ended': True,
        'utterances': [{'id': 'g', 'speaker': '发言人 A', 'raw': raw, 'at': 0}]}
    response = await client.post('/api/v1/meetings/sync', headers=headers, json=payload)
    assert response.status_code == 200, response.text
    session_id = response.json()['data']['sessionId']
    report = {'goals': [{'origin_id': 'g', 'title': TITLE, 'condition': TITLE,
        'milestones': steps if steps is not None else STEPS, 'deadline_text': deadline, 'phase': phase, 'evidence_ids': ['g']}] if with_goal else []}
    async with app.state.database.sessions() as session:
        meeting = await session.get(Meeting, session_id)
        meeting.analysis_revision = meeting.revision
        meeting.analysis = report
        await ProgressService(session).project(meeting, report)
        await session.commit()
    return (await client.get('/api/v1/progress', headers=headers)).json()['data']


async def test_goal_without_deadline_keeps_topology_and_continues_next_meeting(client, app):
    headers = await account(client)
    raw = f'交付目标是{TITLE}。当前阶段项目一期。里程碑包括需求范围确认、核心模块开发、双端联调、上线验收。需求范围确认已经完成，核心模块开发正在进行。'
    first = await sync_and_project(client, app, headers, 'first', raw, phase='项目一期')
    assert len(first['goals']) == 1 and len(first['pending']) == 1
    goal = first['goals'][0]
    assert goal['deadline'] is None
    assert goal['milestones'][0]['status'] == 'completed'
    home = (await client.get('/api/v1/home', headers=headers)).json()['data']
    assert home['situationGoal']['id'] == goal['id']
    assert home['rows'][0]['label'] == '持续目标'
    second = await sync_and_project(client, app, headers, 'second',
        f'继续推进{TITLE}这个目标。核心模块开发已经完成，双端联调正在进行。', with_goal=False)
    assert len(second['goals']) == 1 and second['goals'][0]['id'] == goal['id']
    assert [m['status'] for m in second['goals'][0]['milestones']] == ['completed', 'completed', 'active', 'pending']
    assert len(second['goals'][0]['meetingSources']) == 2
    home = (await client.get('/api/v1/home', headers=headers)).json()['data']
    assert home['situationGoal']['id'] == goal['id']
    # 同一场再次完整提取，不重复建目标或清空旧节点。
    third = await sync_and_project(client, app, headers, 'third',
        f'继续推进{TITLE}这个目标。双端联调已经完成。当前阶段是测试阶段。', phase='测试阶段')
    assert len(third['goals']) == 1
    assert third['goals'][0]['milestones'][0]['status'] == 'completed'
    assert third['goals'][0]['phase'] == '测试阶段'
    other = await account(client, 'another-owner@example.com')
    assert (await client.get('/api/v1/home', headers=other)).json()['data']['situationGoal'] is None


async def test_model_status_suffix_does_not_create_duplicate_milestones(client, app):
    headers = await account(client)
    first = await sync_and_project(client, app, headers, 'suffix-first',
        f'目标是{TITLE}。里程碑包括需求范围确认、核心模块开发、双端联调、上线验收。需求范围确认已经完成，核心模块开发正在进行。')
    second = await sync_and_project(client, app, headers, 'suffix-second',
        f'继续推进{TITLE}这个目标。核心模块开发已经完成，双端联调正在进行，上线验收尚未开始。',
        steps=['核心模块开发已经完成', '双端联调正在进行', '上线验收尚未开始'])
    assert second['goals'][0]['id'] == first['goals'][0]['id']
    nodes = second['goals'][0]['milestones']
    assert [n['title'] for n in nodes] == STEPS
    assert [n['status'] for n in nodes] == ['completed', 'completed', 'active', 'pending']


async def test_similar_meeting_does_not_inherit_unrelated_goal_and_expiry_keeps_history(client, app):
    headers = await account(client)
    first = await sync_and_project(client, app, headers, 'first',
        f'目标是{TITLE}。交期2035年10月30日。里程碑包括需求范围确认、核心模块开发。需求范围确认已经完成。', deadline='2035年10月30日')
    gid = first['goals'][0]['id']
    await sync_and_project(client, app, headers, 'unrelated',
        '今天讨论供应商账务系统，核心模块开发正在进行。', with_goal=False)
    assert (await client.get('/api/v1/home', headers=headers)).json()['data']['situationGoal'] is None
    async with app.state.database.sessions() as session:
        goal = await session.get(ProgressItem, gid)
        goal.overrides = {'deadline': '2020-01-01T00:00:00+08:00'}
        await session.commit()
    data = await sync_and_project(client, app, headers, 'expired-followup',
        f'继续推进{TITLE}这个目标。核心模块开发已经完成。', with_goal=False)
    assert data['goals'][0]['id'] == gid
    assert data['goals'][0]['milestones'][0]['status'] == 'completed'
    assert data['goals'][0]['milestones'][1]['status'] == 'pending'
    assert len(data['goals'][0]['meetingSources']) == 1
    assert (await client.get('/api/v1/home', headers=headers)).json()['data']['situationGoal'] is None


def test_state_labels_and_current_meeting_scope():
    meeting = {'id': 'now', 'content': {'title': '正在讨论的会议', 'utterances': [
        {'id': 'u', 'raw': '建议改期'}]}, 'analysis': {'state': [
            {'topic': '排期', 'value': '建议改期', 'status': 'proposed', 'evidence_ids': ['u']}]}}
    result = build_home([{'id': 'other', 'kind': 'goal', 'title': '另一项目', 'sessionId': 'old',
        'status': 'active', 'deadline': None}], [], meeting)
    assert result['title'] == '正在讨论的会议' and result['situationGoal'] is None
    assert result['rows'][0]['label'] == '待讨论'
    # 有目标也不能用里程碑分组挤掉当前会议的工作态势。
    current = {'id': 'goal', 'kind': 'goal', 'title': TITLE, 'condition': TITLE,
        'sessionId': 'now', 'status': 'active', 'deadline': None,
        'milestones': [{'title': '核心模块开发', 'status': 'active', 'weight': 1}]}
    result = build_home([current], [], meeting)
    assert result['rows'] == [{'label': '待讨论', 'text': '建议改期', 'evidenceIds': ['u']}]


def test_completed_goal_summary_remains_until_deadline_without_reopening_work():
    item = {'id': 'finished', 'kind': 'goal', 'title': TITLE, 'condition': TITLE,
        'status': 'completed', 'deadline': '2031-01-01T00:00:00+00:00',
        'milestones': [{'title': '上线验收', 'weight': 1, 'status': 'completed'}]}
    assert build_home([item], [], now=datetime(2030, 1, 1, tzinfo=timezone.utc))['situationGoal']['id'] == 'finished'
    assert build_home([item], [], now=datetime(2032, 1, 1, tzinfo=timezone.utc))['situationGoal'] is None


async def test_shared_milestone_name_does_not_mix_two_projects(client, app):
    headers = await account(client)
    titles = ['完成甲方工单系统上线验收', '完成乙方仓储系统交付验收']
    utterances = [{'id': f'g{i}', 'speaker': '发言人 A', 'at': i * 1000,
        'raw': f'目标是{title}。里程碑包括核心模块开发。核心模块开发' + ('已经完成。' if i == 0 else '正在进行。')}
        for i, title in enumerate(titles)]
    response = await client.post('/api/v1/meetings/sync', headers=headers, json={
        'id': 'two-projects', 'title': '双项目周会', 'revision': 1,
        'startedAt': '2026-10-25T10:00:00+08:00', 'elapsed': 10000, 'utterances': utterances})
    async with app.state.database.sessions() as session:
        row = await session.get(Meeting, response.json()['data']['sessionId'])
        await ProgressService(session).project(row, {'goals': [{
            'origin_id': f'g{i}', 'title': t, 'condition': t,
            'milestones': ['核心模块开发'], 'evidence_ids': [f'g{i}']}
            for i, t in enumerate(titles)]})
        await session.commit()
    data = (await client.get('/api/v1/progress', headers=headers)).json()['data']
    statuses = {g['title']: g['milestones'][0]['status'] for g in data['goals']}
    assert statuses == {titles[0]: 'completed', titles[1]: 'active'}


async def test_live_progress_source_correction_invalidates_old_claim(client, app):
    headers = await account(client)
    await sync_and_project(client, app, headers, 'source-correction',
        f'目标是{TITLE}。里程碑包括需求范围确认、核心模块开发。')
    async with app.state.database.sessions() as session:
        row = await session.scalar(select(Meeting).where(Meeting.client_id == 'source-correction'))
        row.content = {**row.content, 'utterances': [*row.content['utterances'],
            {'id': 'later', 'speaker': '发言人 A', 'raw': '核心模块开发已经完成。', 'at': 2000}]}
        row.revision += 1
        await session.commit()
    data = (await client.get('/api/v1/progress', headers=headers)).json()['data']
    assert data['goals'][0]['milestones'][1]['status'] == 'completed'
    async with app.state.database.sessions() as session:
        row = await session.scalar(select(Meeting).where(Meeting.client_id == 'source-correction'))
        utterances = [dict(u) for u in row.content['utterances']]
        utterances[-1]['correction'] = '核心模块开发尚未完成。'
        row.content = {**row.content, 'utterances': utterances}
        row.revision += 1
        await session.commit()
    data = (await client.get('/api/v1/progress', headers=headers)).json()['data']
    assert not data['goals']
    assert data['pending'][0]['reason'] == '原话已变更，等待重新核对'


async def test_goal_unlocks_next_stage_across_meetings_and_confirm_clears_pending(client, app):
    headers = await account(client)
    first = await sync_and_project(client, app, headers, 'stage-1',
        f'交付目标是{TITLE}。里程碑包括需求范围确认、核心模块开发、双端联调、上线验收。')
    goal = first['goals'][0]
    assert goal['status'] == 'pending'
    # 用户确认后不再停留在待确认；未提进度时节点保持待完成。
    response = await client.post(f"/api/v1/progress/goals/{goal['id']}/confirm",
        headers=headers, json={'version': goal['version']})
    assert response.status_code == 200, response.text
    confirmed = response.json()['data']
    assert confirmed['status'] == 'active' and confirmed['confirmed'] is True
    again = await client.post(f"/api/v1/progress/goals/{goal['id']}/confirm",
        headers=headers, json={'version': goal['version']})
    assert again.status_code == 200
    second = await sync_and_project(client, app, headers, 'stage-2',
        f'继续推进{TITLE}这个目标。需求范围确认已经完成了，核心模块开发正在进行。', with_goal=False)
    assert [m['status'] for m in second['goals'][0]['milestones']] == ['completed', 'active', 'pending', 'pending']
    third = await sync_and_project(client, app, headers, 'stage-3',
        f'继续推进{TITLE}这个目标。核心模块开发做完了，双端联调已经联调完了，上线验收也通过了。', with_goal=False)
    final = third['goals'][0]
    assert final['id'] == goal['id']
    assert [m['status'] for m in final['milestones']] == ['completed'] * 4
    assert final['status'] == 'completed'
