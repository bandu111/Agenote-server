"""会议原话回收里程碑进度：覆盖口语说法、否定/计划句，以及同场多个目标。"""
from unittest.mock import AsyncMock
import pytest
from test_meetings import account
from test_progress import meeting, report
from app.modules.progress.services.goal_continuity import reported_milestones

MILESTONES = [{'title': t, 'weight': 1, 'status': 'pending'} for t in ['需求确认', '核心开发', '集成测试', '预发部署']]


@pytest.mark.parametrize('text, expected', [
    ('需求确认已经完成。', {'需求确认': 'completed'}),
    ('核心开发这部分，我们已经完成了。', {'核心开发': 'completed'}),
    ('需求确认这块已经搞定了。', {'需求确认': 'completed'}),
    ('核心开发做完了。', {'核心开发': 'completed'}),
    ('按计划核心开发已经完成。', {'核心开发': 'completed'}),
    ('集成测试现在在做，进度一半左右。', {'集成测试': 'active'}),
    ('预发部署昨天弄好了。', {'预发部署': 'completed'}),
    ('核心开发已经完成，集成测试正在进行。', {'核心开发': 'completed', '集成测试': 'active'}),
    ('核心开发这部分，集成测试已经完成了。', {'集成测试': 'completed'}),
    ('计划下周完成核心开发。', {}),
    ('如果核心开发做完了，就开始集成测试。', {}),
    ('核心开发还没做完。', {}),
    ('集成测试现在做不了。', {}),
    ('核心开发这部分，下周再说。', {}),
    ('不要把核心开发标为已完成。', {}),
    ('核心开发做完了吗？', {}),
])
def test_spoken_progress_phrases(text, expected):
    result = reported_milestones(MILESTONES, [{'id': 'u', 'raw': text}], 's')
    assert {m['title']: m['status'] for m in result if m['status'] != 'pending'} == expected


VERB_MILESTONES = [{'title': t, 'weight': 1, 'status': 'pending'}
    for t in ['完成核心功能开发', '接口联调', '提交测试报告', '客户验收']]


@pytest.mark.parametrize('text, expected', [
    # 模型常把节点写成“完成××”，口头只说“××完成了”。
    ('核心功能开发已经完成了。', {'完成核心功能开发': 'completed'}),
    ('核心功能开发正在进行。', {'完成核心功能开发': 'active'}),
    ('接口联调已经联调完了。', {'接口联调': 'completed'}),
    ('测试报告已经提交了。', {'提交测试报告': 'completed'}),
    ('客户验收已经通过了。', {'客户验收': 'completed'}),
    ('客户验收通过了。', {'客户验收': 'completed'}),
    ('测试报告还没提交。', {}),
    ('下周准备提交测试报告。', {}),
    ('测试报告提交了吗？', {}),
])
def test_milestone_with_action_prefix_matches_spoken_progress(text, expected):
    result = reported_milestones(VERB_MILESTONES, [{'id': 'u', 'raw': text}], 's')
    assert {m['title']: m['status'] for m in result if m['status'] != 'pending'} == expected


async def test_meeting_reports_progress_when_two_goals_share_the_meeting(client, app):
    headers = await account(client)
    body = meeting()
    body['utterances'] += [
        {'id': 'g2', 'speaker': '发言人 B', 'at': 1500, 'raw': '交付目标是完成桌面端适配。里程碑包括界面改版、性能优化。'},
        {'id': 'm0', 'speaker': '发言人 A', 'at': 3000, 'raw': '范围确认已经完成。'},
        {'id': 'm1', 'speaker': '发言人 B', 'at': 4000, 'raw': '界面改版正在进行。'},
    ]
    await client.post('/api/v1/meetings/sync', headers=headers, json=body)
    value = report()
    value['goals'].append({'origin_id': 'g2', 'title': '完成桌面端适配', 'condition': '完成桌面端适配',
        'acceptance': '待明确', 'deadline_text': None, 'phase': None,
        'milestones': ['界面改版', '性能优化'], 'evidence_ids': ['g2']})
    app.state.meeting_analysis.agents.analyze = AsyncMock(return_value=value)
    assert (await client.post('/api/v1/meetings/real-progress-session/analysis', headers=headers)).status_code == 200
    goals = {g['title']: {m['title']: m['status'] for m in g['milestones']}
        for g in (await client.get('/api/v1/progress', headers=headers)).json()['data']['goals']}
    assert goals['完成双端测试'] == {'范围确认': 'completed', '双端测试': 'pending'}
    assert goals['完成桌面端适配'] == {'界面改版': 'active', '性能优化': 'pending'}


async def test_shared_milestone_title_stays_scoped_to_its_own_goal(client, app):
    headers = await account(client)
    body = meeting()
    body['utterances'] += [
        {'id': 'g2', 'speaker': '发言人 B', 'at': 1500, 'raw': '交付目标是完成桌面端适配。里程碑包括范围确认、性能优化。'},
        {'id': 'm0', 'speaker': '发言人 B', 'at': 3000, 'raw': '范围确认已经完成。'},
    ]
    await client.post('/api/v1/meetings/sync', headers=headers, json=body)
    value = report()
    value['goals'].append({'origin_id': 'g2', 'title': '完成桌面端适配', 'condition': '完成桌面端适配',
        'acceptance': '待明确', 'deadline_text': None, 'phase': None,
        'milestones': ['范围确认', '性能优化'], 'evidence_ids': ['g2']})
    app.state.meeting_analysis.agents.analyze = AsyncMock(return_value=value)
    await client.post('/api/v1/meetings/real-progress-session/analysis', headers=headers)
    goals = {g['title']: {m['title']: m['status'] for m in g['milestones']}
        for g in (await client.get('/api/v1/progress', headers=headers)).json()['data']['goals']}
    # 两个目标都有“范围确认”，一句无归属的陈述不能同时推进两边。
    assert goals['完成双端测试']['范围确认'] == 'pending'
    assert goals['完成桌面端适配']['范围确认'] == 'pending'
